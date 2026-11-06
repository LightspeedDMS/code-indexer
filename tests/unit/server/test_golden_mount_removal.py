"""Golden-repos mount removal (EVO-64616): cidx needs nothing on the volume.

Each consumer a live check found still touching the golden-repos volume on
a cidx pod is run twice -- LOCAL (today's
layout: golden clones and their indexes on this disk, local IndexStore) and
DELEGATED (a pod whose golden-repos directory holds only what it checked out
of the IndexStore; the repository's own directory ABSENT) -- and must give
the same answer. Under delegated, every local access to golden-repos content,
or to cidx state that used to live there (``.cache``, ``.scratch``), raises
(``forbid_golden_content_io``). Failures are loud: an unpublished index, an
unreachable store or a failed init raise or fail the job, never "absent".
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, cast
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos import snapshot_reader_lease as leases
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepo,
    GoldenRepoManager,
    GoldenRepoNotFoundError,
)
from code_indexer.server.services import index_materializer as im
from code_indexer.server.services.index_materializer import (
    PublishedIndexUnavailableError,
)
from code_indexer.server.startup import index_store_wiring
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore
from code_indexer.utils.index_store import (
    DelegatedIndexStore,
    LocalIndexStore,
    index_store_alias,
    published_index_fields,
)
from tests.unit.server.activated_seam_fakes import LocalIOForbidden
from tests.unit.server.golden_mount_fakes import (
    INDEX_DIR,
    forbid_golden_content_io,
    guard_is_live,
)
from tests.unit.server.index_store_fakes import FakeArtifacts

ALIAS = "example-repo"
GLOBAL = f"{ALIAS}-global"


# ── fixtures ────────────────────────────────────────────────────────────────


def _write_index(code_indexer: Path, *, rows: int = 3, fts: bool = True) -> None:
    coll = code_indexer / "index" / "voyage-code-3"
    coll.mkdir(parents=True)
    (coll / "collection_meta.json").write_text('{"name": "x", "vector_size": 4}')
    store = ChunkStore(coll / "chunks.db")
    try:
        if rows:
            store.write_batch(
                [{"id": f"p{i}", "vector": [0.1, 0.2, 0.3, 0.4]} for i in range(rows)]
            )
    finally:
        store.close()
    write_chunks_db_discriminator(coll)
    if fts:
        (code_indexer / "tantivy_index").mkdir()
        (code_indexer / "tantivy_index" / "meta.json").write_text("{}")
    (code_indexer / "config.json").write_text('{"embedding_provider": "voyage-ai"}')


def _manager(golden_repos_dir: Path) -> GoldenRepoManager:
    manager = GoldenRepoManager.__new__(GoldenRepoManager)
    manager.golden_repos_dir = str(golden_repos_dir)
    manager.golden_repos = {
        ALIAS: GoldenRepo(
            alias=ALIAS,
            repo_url="https://example.com/example-repo.git",
            default_branch="main",
            clone_path=str(golden_repos_dir / ALIAS),
            created_at="2024-01-01T00:00:00Z",
        )
    }
    return manager


class World:
    """A local deployment and a mount-less pod over one published index."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.local_golden = tmp_path / "local" / "data" / "golden-repos"
        (self.local_golden / ALIAS).mkdir(parents=True)
        (self.local_golden / ALIAS / "README.md").write_text("content\n")
        _write_index(self.local_golden / ALIAS / INDEX_DIR)

        self.pod_golden = tmp_path / "pod" / "data" / "golden-repos"
        self.pod_golden.mkdir(parents=True)
        self.artifacts = FakeArtifacts(tmp_path / "store")
        self.store = DelegatedIndexStore(self.artifacts)
        built = tmp_path / "publisher" / INDEX_DIR
        _write_index(built)
        self.version = self.store.publish_as(index_store_alias(ALIAS), built)
        monkeypatch.setattr(im, "run_fix_config", lambda target: None)

    @property
    def pod_clone(self) -> Path:
        return self.pod_golden / ALIAS

    def local(self) -> None:
        self.monkeypatch.setattr(
            index_store_wiring, "server_index_store", lambda: LocalIndexStore()
        )

    def delegated(self, *, record_version: bool = True) -> None:
        self.monkeypatch.setattr(
            index_store_wiring, "server_index_store", lambda: self.store
        )
        extra = (
            published_index_fields(index_store_alias(ALIAS), self.version)
            if record_version
            else None
        )
        AliasManager(str(self.pod_golden / "aliases")).create_alias(
            GLOBAL, str(self.pod_clone), repo_name=ALIAS, extra=extra
        )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


def test_the_guard_is_live(world: World) -> None:
    (world.pod_clone).mkdir()
    (world.pod_clone / "README.md").write_text("x")
    assert guard_is_live(world.pod_golden, ALIAS) == []


# ── 1. index-existence checks ───────────────────────────────────────────────


def _flags(result: Dict[str, Any]) -> Dict[str, bool]:
    return {k: v["exists"] for k, v in result["indexes"].items()}


def test_golden_repo_indexes_delegated_matches_local_with_the_pod_path_absent(
    world: World,
) -> None:
    world.local()
    local = _flags(_manager(world.local_golden).get_golden_repo_indexes(ALIAS))

    world.delegated()
    assert not world.pod_clone.exists()
    manager = _manager(world.pod_golden)
    with forbid_golden_content_io(world.pod_golden):
        delegated = _flags(manager.get_golden_repo_indexes(ALIAS))
    assert delegated == local
    assert local["semantic"] is True and local["fts"] is True


def test_index_exists_reads_the_served_checkout_not_an_absent_dir(
    world: World,
) -> None:
    world.delegated()
    manager = _manager(world.pod_golden)
    with forbid_golden_content_io(world.pod_golden):
        assert manager._index_exists(manager.golden_repos[ALIAS], "semantic") is True
    assert (world.pod_clone / INDEX_DIR / "index").is_dir()


def test_an_unpublished_pointer_fails_loud_never_absent(world: World) -> None:
    world.delegated(record_version=False)
    manager = _manager(world.pod_golden)
    with pytest.raises(PublishedIndexUnavailableError):
        with forbid_golden_content_io(world.pod_golden):
            manager.get_golden_repo_indexes(ALIAS)


def test_no_alias_pointer_under_delegated_is_not_found(world: World) -> None:
    world.monkeypatch.setattr(
        index_store_wiring, "server_index_store", lambda: world.store
    )
    manager = _manager(world.pod_golden)
    with pytest.raises(GoldenRepoNotFoundError, match="no alias pointer"):
        with forbid_golden_content_io(world.pod_golden):
            manager._index_exists(manager.golden_repos[ALIAS], "fts")


def test_a_store_outage_fails_loud(world: World) -> None:
    world.delegated()
    world.artifacts.fail_checkout = True
    manager = _manager(world.pod_golden)
    with pytest.raises(PublishedIndexUnavailableError):
        manager.get_golden_repo_indexes(ALIAS)


def test_the_indexed_gauge_reads_the_registry_without_checking_out(
    world: World,
) -> None:
    world.local()
    assert _manager(world.local_golden).counts_as_indexed(
        _manager(world.local_golden).golden_repos[ALIAS]
    )
    world.delegated()
    manager = _manager(world.pod_golden)
    world.artifacts.calls.clear()
    with forbid_golden_content_io(world.pod_golden):
        assert manager.counts_as_indexed(manager.golden_repos[ALIAS]) is True
    assert world.artifacts.calls == []
    assert not world.pod_clone.exists()


def test_the_indexed_gauge_counts_an_unpublished_repo_as_not_indexed(
    world: World,
) -> None:
    world.delegated(record_version=False)
    manager = _manager(world.pod_golden)
    assert manager.counts_as_indexed(manager.golden_repos[ALIAS]) is False


def test_a_local_gauge_still_reads_the_disk(world: World) -> None:
    world.local()
    manager = _manager(world.local_golden)
    shutil.rmtree(world.local_golden / ALIAS / INDEX_DIR)
    assert manager.counts_as_indexed(manager.golden_repos[ALIAS]) is False


# ── 1b. refresh START reconcile ─────────────────────────────────────────────


def _scheduler(world: World, golden: Path, store: Any) -> Any:
    return SimpleNamespace(
        _index_store=store,
        alias_manager=AliasManager(str(golden / "aliases")),
    )


def test_start_reconcile_serves_the_published_target_first(world: World) -> None:
    world.delegated()
    fake = _scheduler(world, world.pod_golden, world.store)
    with forbid_golden_content_io(world.pod_golden):
        target = RefreshScheduler._start_reconcile_target(
            fake, GLOBAL, str(world.pod_clone)
        )
    assert target == world.pod_clone
    assert (world.pod_clone / INDEX_DIR / "index").is_dir()


def test_start_reconcile_skips_when_nothing_is_here_or_published(
    world: World,
) -> None:
    world.delegated(record_version=False)
    fake = _scheduler(world, world.pod_golden, world.store)
    with forbid_golden_content_io(world.pod_golden):
        target = RefreshScheduler._start_reconcile_target(
            fake, GLOBAL, str(world.pod_clone)
        )
    assert target is None
    assert world.artifacts.calls[-1][0] == "publish"  # nothing checked out


def test_start_reconcile_reads_an_unrecorded_index_this_pod_holds(
    world: World,
) -> None:
    world.delegated(record_version=False)
    _write_index(world.pod_clone / INDEX_DIR)
    fake = _scheduler(world, world.pod_golden, world.store)
    assert (
        RefreshScheduler._start_reconcile_target(fake, GLOBAL, str(world.pod_clone))
        == world.pod_clone
    )


def test_start_reconcile_is_unchanged_locally(world: World) -> None:
    fake = _scheduler(world, world.local_golden, LocalIndexStore())
    target = str(world.local_golden / ALIAS)
    assert RefreshScheduler._start_reconcile_target(fake, GLOBAL, target) == Path(
        target
    )


# ── 4. cidx-meta refresh on a pod that never held it ────────────────────────


META = "cidx-meta"


class _MetaScheduler:
    """The pieces _prepare_delegated_cidx_meta uses, the real methods bound."""

    def __init__(self, store: Any, init_ok: bool = True) -> None:
        self._index_store = store
        self.seeded: List[str] = []
        self.inits: List[str] = []
        self._init_ok = init_ok

    def _seed_incremental_base(self, alias_name: str, source_path: str) -> None:
        self.seeded.append(source_path)

    def _repair_uninitialized_local_repo(
        self, source_path: str, alias_name: str, cancel_check: Any = None
    ) -> Any:
        self.inits.append(source_path)
        if self._init_ok:
            (Path(source_path) / INDEX_DIR).mkdir()
            return True, None
        return False, "cidx init exploded"

    def prepare(self, *args: Any) -> Any:
        return RefreshScheduler._prepare_delegated_cidx_meta(cast(Any, self), *args)


class _SeedingMetaScheduler(_MetaScheduler):
    """The store holds a published version: seeding checks it out."""

    def _seed_incremental_base(self, alias_name: str, source_path: str) -> None:
        self.seeded.append(source_path)
        (Path(source_path) / INDEX_DIR).mkdir(parents=True)


def test_cidx_meta_is_seeded_from_the_store(world: World) -> None:
    world.delegated()
    source = world.pod_golden / META
    fake = _SeedingMetaScheduler(world.store)
    with forbid_golden_content_io(world.pod_golden):
        assert (
            fake.prepare("cidx-meta-global", "local://cidx-meta", META, str(source))
            is None
        )
    assert fake.seeded == [str(source)] and fake.inits == []


def test_cidx_meta_with_nothing_published_is_initialized_here(world: World) -> None:
    world.delegated()
    source = world.pod_golden / META
    fake = _MetaScheduler(world.store)
    with forbid_golden_content_io(world.pod_golden):
        assert (
            fake.prepare("cidx-meta-global", "local://cidx-meta", META, str(source))
            is None
        )
    assert fake.inits == [str(source)]
    assert (source / INDEX_DIR).is_dir()


def test_a_failed_cidx_meta_init_fails_the_refresh(world: World) -> None:
    world.delegated()
    fake = _MetaScheduler(world.store, init_ok=False)
    result = fake.prepare(
        "cidx-meta-global", "local://cidx-meta", META, str(world.pod_golden / META)
    )
    assert result is not None and result["success"] is False
    assert "cidx init exploded" in result["message"]


@pytest.mark.parametrize(
    "store_kind, repo_url, name",
    [
        ("local", "local://cidx-meta", META),
        ("delegated", "local://langfuse-x", "langfuse-x"),
    ],
)
def test_cidx_meta_preparation_is_inert_otherwise(
    world: World, store_kind: str, repo_url: str, name: str
) -> None:
    store = LocalIndexStore() if store_kind == "local" else world.store
    fake = _MetaScheduler(store)
    source = world.pod_golden / name
    assert fake.prepare(f"{name}-global", repo_url, name, str(source)) is None
    assert fake.seeded == [] and fake.inits == []
    assert not source.exists()


# ── 2. golden add: no wait on the mount ─────────────────────────────────────


def _post_clone_manager(golden: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    manager = _manager(golden)
    calls: List[str] = []
    monkeypatch.setattr(
        manager,
        "_await_clone_path_locally_visible",
        lambda path: calls.append(path),
        raising=False,
    )
    return manager, calls


def _stub_children(monkeypatch: pytest.MonkeyPatch, spawned: List[Any]) -> None:
    from code_indexer.server.repositories import golden_repo_manager as grm
    from code_indexer.services import progress_subprocess_runner as psr
    from code_indexer.server.services import fts_index_builder

    def run(cmd: Any, **kwargs: Any) -> Any:
        spawned.append((list(cmd), kwargs.get("cwd")))
        Path(kwargs["cwd"], INDEX_DIR).mkdir(exist_ok=True)
        Path(kwargs["cwd"], INDEX_DIR, "config.json").write_text("{}")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(grm.subprocess, "run", run)
    monkeypatch.setattr(
        psr,
        "run_with_popen_progress",
        lambda **kw: spawned.append((list(kw["command"]), kw["cwd"])),
    )
    monkeypatch.setattr(psr, "gather_repo_metrics", lambda *a, **k: (0, 0))
    monkeypatch.setattr(
        fts_index_builder,
        "ensure_fts_index",
        lambda *a, **k: SimpleNamespace(
            ok=True, status=SimpleNamespace(value="present"), detail=""
        ),
    )
    config_service = MagicMock()
    monkeypatch.setattr(
        "code_indexer.server.services.config_service.get_config_service",
        lambda: config_service,
    )
    monkeypatch.setattr(
        "code_indexer.server.storage.postgres.embedding_stats_child_wiring."
        "build_embedding_stats_child_env",
        lambda *a, **k: dict(os.environ),
    )


def test_a_delegated_add_builds_in_its_index_state_dir_without_probing_the_mount(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, waits = _post_clone_manager(world.pod_golden, monkeypatch)
    monkeypatch.setattr(manager, "repositories_are_delegated", lambda: True)
    spawned: List[Any] = []
    _stub_children(monkeypatch, spawned)
    world.pod_clone.mkdir()  # the index-state dir _clone_for_delegated_content made
    with forbid_golden_content_io(world.pod_golden):
        manager._execute_post_clone_workflow(
            str(world.pod_clone), golden_repo_alias=ALIAS
        )
    assert waits == []
    assert spawned and all(cwd == str(world.pod_clone) for _, cwd in spawned)


@pytest.mark.parametrize("delegated, local_content", [(False, False), (True, True)])
def test_the_mount_barrier_still_runs_where_the_clone_is_on_this_pod(
    world: World, monkeypatch: pytest.MonkeyPatch, delegated: bool, local_content: bool
) -> None:
    manager, waits = _post_clone_manager(world.local_golden, monkeypatch)
    monkeypatch.setattr(manager, "repositories_are_delegated", lambda: delegated)
    _stub_children(monkeypatch, [])
    clone = str(world.local_golden / ALIAS)
    manager._execute_post_clone_workflow(
        clone, golden_repo_alias=ALIAS, local_content=local_content
    )
    assert waits == [clone]


# ── 5. server state off the volume ──────────────────────────────────────────


def test_pod_local_leases_never_touch_golden_repos(
    world: World, tmp_path: Path
) -> None:
    pod_leases = tmp_path / "pod" / "data" / ".pod-state" / "snapshot-reader-leases"
    lease_root = world.pod_golden / "cidx-meta"
    snapshot = str(world.pod_golden / ".versioned" / ALIAS / "v_1")
    leases.use_pod_local_lease_directory(pod_leases)
    try:
        with forbid_golden_content_io(world.pod_golden):
            assert leases.primary_lease_directory(lease_root) == pod_leases
            pod_leases.mkdir(parents=True)
            lease = leases.SnapshotReaderLease(snapshot, 60, lease_root=lease_root)
            lease.acquire()
            assert leases.snapshot_has_live_reader(snapshot, lease_root=lease_root)
            lease.release()
            assert not leases.snapshot_has_live_reader(snapshot, lease_root=lease_root)
    finally:
        leases.use_pod_local_lease_directory(None)
    assert not (world.pod_golden / ".scratch").exists()


def test_leases_are_unchanged_by_default(world: World) -> None:
    lease_root = world.local_golden / "cidx-meta"
    assert leases.primary_lease_directory(lease_root) == (
        world.local_golden / ".scratch" / "snapshot-reader-leases"
    )
    legacy = lease_root / ".snapshot-reader-leases"
    legacy.mkdir(parents=True)
    snapshot = str(world.local_golden / ".versioned" / ALIAS / "v_1")
    (legacy / "x.json").write_text(
        '{"snapshot_path": "%s", "updated_at": 9e12, "ttl_seconds": 60}'
        % str(Path(snapshot).resolve())
    )
    # the legacy in-tree lease still counts during a rolling upgrade
    assert leases.snapshot_has_live_reader(snapshot, lease_root=lease_root)


# ── 6. background sweeps ────────────────────────────────────────────────────


def _backfill_manager(golden: Path) -> Any:
    return SimpleNamespace(golden_repos_dir=str(golden))


def test_the_fts_backfill_is_disabled_under_a_delegated_store(world: World) -> None:
    from code_indexer.server.services.fts_backfill.sweep import run_fts_backfill_cycle

    world.delegated()
    world.pod_clone.mkdir()
    jobs = MagicMock()
    with forbid_golden_content_io(world.pod_golden):
        counts = run_fts_backfill_cycle(
            _backfill_manager(world.pod_golden),
            jobs,
            batch_size=5,
            cooldown_seconds=0,
        )
    assert counts.scanned == 0 and counts.enqueued == 0
    jobs.submit_job.assert_not_called()


def test_the_fts_backfill_still_runs_locally(world: World) -> None:
    from code_indexer.server.services.fts_backfill.sweep import run_fts_backfill_cycle

    world.local()
    shutil.rmtree(world.local_golden / ALIAS / INDEX_DIR / "tantivy_index")
    jobs = MagicMock()
    counts = run_fts_backfill_cycle(
        _backfill_manager(world.local_golden), jobs, batch_size=5, cooldown_seconds=0
    )
    assert counts.enqueued == 1
    jobs.submit_job.assert_called_once()


def test_the_semantic_backfill_is_disabled_under_a_delegated_store(
    world: World,
) -> None:
    from code_indexer.server.services.semantic_backfill.sweep import (
        run_semantic_backfill_cycle,
    )

    world.delegated()
    world.pod_clone.mkdir()
    jobs = MagicMock()
    with forbid_golden_content_io(world.pod_golden):
        counts = run_semantic_backfill_cycle(
            _backfill_manager(world.pod_golden),
            jobs,
            enabled=True,
            batch_size=5,
            cooldown_seconds=0,
        )
    assert counts.scanned == 0
    jobs.submit_job.assert_not_called()


def test_queued_backfill_builds_do_not_build_in_place(world: World) -> None:
    from code_indexer.server.services.fts_backfill.policy import (
        fts_backfill_build_executor,
    )
    from code_indexer.server.services.semantic_backfill.policy import (
        semantic_backfill_reindex_executor,
    )

    world.delegated()
    golden = MagicMock()
    with forbid_golden_content_io(world.pod_golden):
        fts = fts_backfill_build_executor(
            {"repo_path": str(world.pod_clone), "alias": ALIAS}
        )
        semantic = semantic_backfill_reindex_executor(
            golden, {"clone_path": str(world.pod_clone), "alias": ALIAS}
        )
    assert fts["status"] == semantic["status"] == "skipped"
    assert "index_store_backend='delegated'" in fts["detail"]
    golden._execute_post_clone_workflow.assert_not_called()


def _stale_rows(golden: Path) -> List[Any]:
    return [({"alias": ALIAS}, str(golden / ALIAS))]


def _stale_index_inputs(monkeypatch: pytest.MonkeyPatch) -> Any:
    from code_indexer.server.services import golden_repo_reconciler as rec

    monkeypatch.setattr(rec, "read_current_commit", lambda path: "a" * 40)
    monkeypatch.setattr(rec, "_get_clone_git_head", lambda path, **k: "b" * 40)
    monkeypatch.setattr(rec, "_repositories_delegated", lambda m: False)
    manager = MagicMock()
    manager.is_globally_active.return_value = True
    return rec, manager


def test_the_gap4_staleness_pass_is_disabled_under_a_delegated_store(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.delegated()
    rec, manager = _stale_index_inputs(monkeypatch)
    scheduler = MagicMock()
    result = rec.ReconcileResult()
    rec._reconcile_stale_indexes(
        scheduler, manager, _stale_rows(world.pod_golden), "system", result
    )
    scheduler.trigger_refresh_for_repo.assert_not_called()
    assert result.stale_indexes_found == []


def test_the_gap4_staleness_pass_still_refreshes_locally(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.local()
    rec, manager = _stale_index_inputs(monkeypatch)
    scheduler = MagicMock()
    result = rec.ReconcileResult()
    rec._reconcile_stale_indexes(
        scheduler, manager, _stale_rows(world.local_golden), "system", result
    )
    scheduler.trigger_refresh_for_repo.assert_called_once()
    assert result.stale_indexes_found == [ALIAS]


def test_forbidden_access_raises_inside_the_guard(world: World) -> None:
    world.pod_clone.mkdir()
    with pytest.raises(LocalIOForbidden):
        with forbid_golden_content_io(world.pod_golden):
            (world.pod_golden / ".cache" / "payload_cache.db").exists()


def test_a_disabled_sweep_logs_its_reason_once_per_process(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from code_indexer.server.services import golden_index_backfill
    from code_indexer.server.services.fts_backfill.sweep import run_fts_backfill_cycle

    world.delegated()
    world.monkeypatch.setattr(golden_index_backfill, "_disabled_logged", set())
    caplog.set_level(logging.DEBUG, logger=golden_index_backfill.__name__)
    for _ in range(3):
        run_fts_backfill_cycle(
            _backfill_manager(world.pod_golden),
            MagicMock(),
            batch_size=5,
            cooldown_seconds=0,
        )
    disabled = [r for r in caplog.records if "FTS backfill sweep disabled" in r.message]
    assert [r.levelno for r in disabled] == [logging.INFO, logging.DEBUG, logging.DEBUG]
