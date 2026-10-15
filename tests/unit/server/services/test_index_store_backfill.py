"""The one-off IndexStore backfill after rollout.

* golden: every ``-global`` pointer with no recorded version gets a FORCED
  refresh (which publishes and records it); recorded ones, ``local://`` repos
  and unregistered aliases are left alone and reported; a refresh failure is
  reported with its cause;
* activated: a copy whose index THIS pod holds is published as is (over any
  leftover) and recorded; one held nowhere is rebuilt only on request (seeded
  from golden, clear reindex for the owner) and otherwise reported, never
  skipped silently; a per-copy failure is reported; a registry LISTING failure
  aborts the run;
* ``dry_run`` changes nothing; the local IndexStore refuses;
* the admin endpoint is wired to the real managers and maps refusal to 409.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services import index_materializer as im
from code_indexer.server.repositories.background_jobs import (
    DuplicateJobError,
)
from code_indexer.server.services.index_store_backfill import (
    BACKFILL_REBUILD_MARKER,
    REBUILD_INDEX_TYPES,
    BackfillRefusedError,
    IndexStoreBackfill,
)
from code_indexer.server.startup import index_store_wiring
from code_indexer.utils.index_store import (
    INDEX_STORE_ALIAS_FIELD,
    INDEX_VERSION_FIELD,
    DelegatedIndexStore,
    LocalIndexStore,
    activated_index_store_alias,
)
from tests.unit.server.activated_seam_fakes import (
    ALIAS,
    INDEX_DIR,
    USER,
    ActivatedWorld,
)
from tests.unit.server.index_store_fakes import FakeArtifacts

RECORDED = {INDEX_STORE_ALIAS_FIELD: "x", INDEX_VERSION_FIELD: "v1"}


def _backfill(store: Any, **kw: Any) -> IndexStoreBackfill:
    arm = kw.get("arm")
    if arm is None:
        arm = MagicMock()
        arm.list_all_activated_repositories.return_value = []
    return IndexStoreBackfill(
        store=store,
        alias_records=kw.get("records", lambda: {}),
        golden_repos=kw.get("golden", lambda: {}),
        refresh=kw.get("refresh", MagicMock(return_value="job")),
        activated_repo_manager=arm,
        index_manager=kw.get("index_manager", MagicMock()),
        pod="pod-b",
    )


def _delegated(tmp_path: Path) -> DelegatedIndexStore:
    return DelegatedIndexStore(FakeArtifacts(tmp_path / "store"))


# ── golden ──────────────────────────────────────────────────────────────────


def test_golden_pointers_without_a_version_get_a_forced_refresh(
    tmp_path: Path,
) -> None:
    refresh = MagicMock(side_effect=lambda alias, who: f"job-{alias}")
    report = _backfill(
        _delegated(tmp_path),
        records=lambda: {
            "a-global": {"target_path": "/g/a"},
            "b-global": {"target_path": "/g/b", **RECORDED},
            "c-global": {"target_path": "/g/c"},
            "d-global": {"target_path": "/g/d"},
            "temporal-sister": {"target_path": "/t"},
        },
        golden=lambda: {
            "a": "https://x/a.git",
            "b": "https://x/b.git",
            "c": "local://c",
        },
        refresh=refresh,
    ).run(submitter="admin", dry_run=False)
    assert report.golden_refreshed == {"a-global": "job-a-global"}
    refresh.assert_called_once_with("a-global", "admin")
    assert set(report.golden_skipped) == {"c-global", "d-global"}
    assert "local://" in report.golden_skipped["c-global"]


def test_a_failed_refresh_is_reported_with_its_cause(tmp_path: Path) -> None:
    report = _backfill(
        _delegated(tmp_path),
        records=lambda: {"a-global": {"target_path": "/g/a"}},
        golden=lambda: {"a": "https://x/a.git"},
        refresh=MagicMock(side_effect=ValueError("not in registry")),
    ).run(submitter="admin", dry_run=False)
    assert report.golden_failed == {"a-global": "ValueError: not in registry"}


def test_a_dry_run_refreshes_nothing(tmp_path: Path) -> None:
    refresh = MagicMock()
    report = _backfill(
        _delegated(tmp_path),
        records=lambda: {"a-global": {"target_path": "/g/a"}},
        golden=lambda: {"a": "https://x/a.git"},
        refresh=refresh,
    ).run(submitter="admin")
    assert report.dry_run and report.golden_refreshed == {"a-global": None}
    refresh.assert_not_called()


def test_an_unreadable_alias_store_aborts(tmp_path: Path) -> None:
    def broken() -> Dict[str, Any]:
        raise ConnectionError("registry down")

    with pytest.raises(ConnectionError):
        _backfill(_delegated(tmp_path), records=broken).run(submitter="admin")


def test_the_local_store_refuses() -> None:
    with pytest.raises(BackfillRefusedError):
        _backfill(LocalIndexStore()).run(submitter="admin", dry_run=False)


# ── activated ───────────────────────────────────────────────────────────────


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ActivatedWorld:
    return ActivatedWorld(tmp_path, monkeypatch)


@pytest.fixture
def artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeArtifacts:
    fake = FakeArtifacts(tmp_path / "artifacts")
    store = DelegatedIndexStore(fake)
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: store)
    monkeypatch.setattr(im, "run_fix_config", lambda target: None)
    return fake


def _legacy_copy(world: ActivatedWorld, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A copy activated before publishing existed: no recorded version."""
    monkeypatch.setattr(
        index_store_wiring, "server_index_store", lambda: LocalIndexStore()
    )
    manager = world.delegated_manager()
    manager.golden_repo_manager.golden_repos_dir = str(world.pod_data / "golden-repos")
    manager._do_activate_repository(
        username=USER, golden_repo_alias=ALIAS, branch_name="main", user_alias=ALIAS
    )
    return manager


def _run(manager: Any, artifacts: FakeArtifacts, **kw: Any) -> Any:
    index_manager = kw.pop("index_manager", MagicMock())
    return _backfill(
        DelegatedIndexStore(artifacts), arm=manager, index_manager=index_manager
    ).run(submitter="admin", **kw)


def test_a_copy_held_here_is_published_and_recorded(
    world: ActivatedWorld, artifacts: FakeArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _legacy_copy(world, monkeypatch)
    monkeypatch.setattr(
        index_store_wiring,
        "server_index_store",
        lambda: DelegatedIndexStore(artifacts),
    )
    alias = activated_index_store_alias(USER, ALIAS)
    leftover = world.tmp / "leftover" / INDEX_DIR
    leftover.mkdir(parents=True)
    DelegatedIndexStore(artifacts).publish_as(alias, leftover)
    with world.forbid_local_io():
        report = _run(manager, artifacts, dry_run=False)
    assert report.activated_published == [f"{USER}/{ALIAS}"]
    meta = manager.registration(USER, ALIAS)
    assert meta[INDEX_VERSION_FIELD] == artifacts.current_version(alias)
    # idempotent
    again = _run(manager, artifacts, dry_run=False)
    assert again.activated_published == [] and again.activated_not_held_here == []


def test_a_copy_held_nowhere_is_reported_unless_rebuilt(
    world: ActivatedWorld, artifacts: FakeArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _legacy_copy(world, monkeypatch)
    monkeypatch.setattr(
        index_store_wiring,
        "server_index_store",
        lambda: DelegatedIndexStore(artifacts),
    )
    shutil.rmtree(world.pod_copy())  # pod B
    report = _run(manager, artifacts, dry_run=False)
    assert report.activated_not_held_here == [f"{USER}/{ALIAS}"]
    assert report.activated_published == [] and report.activated_rebuilt == {}

    index_manager = MagicMock()
    index_manager.trigger_reindex.return_value = "job-7"
    with world.forbid_local_io():
        rebuilt = _run(
            manager,
            artifacts,
            dry_run=False,
            rebuild_missing=True,
            index_manager=index_manager,
        )
    assert rebuilt.activated_rebuilt == {f"{USER}/{ALIAS}": "job-7"}
    index_manager.trigger_reindex.assert_called_once_with(
        ALIAS, list(REBUILD_INDEX_TYPES), True, USER
    )
    seeded = world.pod_copy() / INDEX_DIR / "config.json"
    assert (
        seeded.read_text() == (world.pod_golden / INDEX_DIR / "config.json").read_text()
    )


def test_a_dry_run_publishes_and_rebuilds_nothing(
    world: ActivatedWorld, artifacts: FakeArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _legacy_copy(world, monkeypatch)
    monkeypatch.setattr(
        index_store_wiring,
        "server_index_store",
        lambda: DelegatedIndexStore(artifacts),
    )
    report = _run(manager, artifacts)
    assert report.activated_published == [f"{USER}/{ALIAS}"]
    assert artifacts.calls == []
    assert INDEX_VERSION_FIELD not in manager.registration(USER, ALIAS)


def test_a_per_copy_failure_is_reported(
    world: ActivatedWorld, artifacts: FakeArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _legacy_copy(world, monkeypatch)
    monkeypatch.setattr(
        index_store_wiring,
        "server_index_store",
        lambda: DelegatedIndexStore(artifacts),
    )
    artifacts.fail_publish = True
    report = _run(manager, artifacts, dry_run=False)
    assert list(report.activated_failed) == [f"{USER}/{ALIAS}"]
    assert "unreachable" in report.activated_failed[f"{USER}/{ALIAS}"]


def test_a_registry_listing_failure_aborts(tmp_path: Path) -> None:
    arm = MagicMock()
    arm.list_all_activated_repositories.side_effect = ConnectionError("pg down")
    with pytest.raises(ConnectionError):
        _backfill(_delegated(tmp_path), arm=arm).run(submitter="admin")


def test_composites_are_never_rebuilt(tmp_path: Path) -> None:
    arm = MagicMock()
    arm.list_all_activated_repositories.return_value = [
        {"username": USER, "user_alias": "combo"}
    ]
    arm.registration.return_value = {"is_composite": True}
    arm.get_activated_repo_path.return_value = str(tmp_path / "nowhere")
    report = _backfill(_delegated(tmp_path), arm=arm).run(
        submitter="admin", dry_run=False, rebuild_missing=True
    )
    assert report.activated_not_held_here == [f"{USER}/combo"]
    arm.seed_index_from_golden.assert_not_called()


# ── the admin endpoint ──────────────────────────────────────────────────────


def _app(state: Any) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from code_indexer.server.auth import dependencies
    from code_indexer.server.routers import index_store_admin

    app = FastAPI()
    app.include_router(index_store_admin.router)
    for key, value in state.items():
        setattr(app.state, key, value)
    admin = MagicMock()
    admin.username = "admin"
    app.dependency_overrides[dependencies.get_current_admin_user_hybrid] = lambda: admin
    for dep in getattr(index_store_admin.router.routes[0], "dependencies", []):
        app.dependency_overrides[dep.dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _state(store: Any, scheduler: Optional[Any], root: Path) -> Dict[str, Any]:
    golden = MagicMock()
    golden.golden_repos_dir = str(root / "golden-repos")
    golden.list_golden_repos.return_value = [{"alias": "a", "repo_url": "https://x"}]
    arm = MagicMock()
    arm.list_all_activated_repositories.return_value = []
    lifecycle = MagicMock()
    lifecycle.refresh_scheduler = scheduler
    return {
        "golden_repo_manager": golden,
        "activated_repo_manager": arm,
        "global_lifecycle_manager": lifecycle,
        "background_job_manager": MagicMock(),
        "index_store": store,
    }


def test_the_endpoint_refuses_under_the_local_store(tmp_path: Path) -> None:
    response = _app(_state(LocalIndexStore(), MagicMock(), tmp_path)).post(
        "/api/admin/index-store/backfill", json={"dry_run": False}
    )
    assert response.status_code == 409


def test_the_endpoint_needs_a_refresh_scheduler(tmp_path: Path) -> None:
    response = _app(_state(_delegated(tmp_path), None, tmp_path)).post(
        "/api/admin/index-store/backfill", json={}
    )
    assert response.status_code == 503


def test_the_endpoint_forces_refreshes_through_the_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.global_repos.alias_manager import AliasManager

    monkeypatch.setattr(
        AliasManager,
        "list_records",
        lambda self, prefix="": {"a-global": {"target_path": "/g/a"}},
    )
    scheduler = MagicMock()
    scheduler.trigger_refresh_for_repo.return_value = "job-1"
    response = _app(_state(_delegated(tmp_path), scheduler, tmp_path)).post(
        "/api/admin/index-store/backfill", json={"dry_run": False}
    )
    assert response.status_code == 200, response.text
    assert response.json()["golden_refreshed"] == {"a-global": "job-1"}
    scheduler.trigger_refresh_for_repo.assert_called_once_with(
        "a-global", submitter_username="admin", force_reset=True
    )


def test_an_unexpected_failure_is_a_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.global_repos.alias_manager import AliasManager

    def broken(self: Any, prefix: str = "") -> Dict[str, Any]:
        raise ConnectionError("registry down")

    monkeypatch.setattr(AliasManager, "list_records", broken)
    response = _app(_state(_delegated(tmp_path), MagicMock(), tmp_path)).post(
        "/api/admin/index-store/backfill", json={}
    )
    assert response.status_code == 500
    assert "registry down" in response.json()["detail"]


def test_list_records_returns_whole_documents(tmp_path: Path) -> None:
    from code_indexer.global_repos.alias_manager import AliasManager

    aliases = AliasManager(str(tmp_path / "golden-repos" / "aliases"))
    aliases.create_alias("a-global", "/g/a", extra=RECORDED)
    records: List[Any] = list(aliases.list_records().values())
    assert records[0][INDEX_VERSION_FIELD] == "v1"


def test_a_pointer_with_an_alias_but_no_version_is_refreshed(tmp_path: Path) -> None:
    report = _backfill(
        _delegated(tmp_path),
        records=lambda: {
            "a-global": {"target_path": "/g/a", INDEX_STORE_ALIAS_FIELD: "a"}
        },
        golden=lambda: {"a": "https://x/a.git"},
    ).run(submitter="admin", dry_run=False)
    assert report.golden_refreshed == {"a-global": "job"}


def test_failed_trigger_reindex_cleans_up_seeded_index(
    world: ActivatedWorld, artifacts: FakeArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When trigger_reindex raises, the seeded index is removed so a re-run
    cannot publish it with fresh=True."""
    manager = _legacy_copy(world, monkeypatch)
    monkeypatch.setattr(
        index_store_wiring,
        "server_index_store",
        lambda: DelegatedIndexStore(artifacts),
    )
    shutil.rmtree(world.pod_copy())  # pod B — no copy
    index_manager = MagicMock()
    index_manager.trigger_reindex.side_effect = ValueError(
        "Another reindex job is already running/pending (job 99). "
        "Please wait for it to complete before starting a new reindex."
    )
    with world.forbid_local_io():
        report = _run(
            manager,
            artifacts,
            dry_run=False,
            rebuild_missing=True,
            index_manager=index_manager,
        )
    assert report.activated_already_queued == {f"{USER}/{ALIAS}": index_manager.trigger_reindex.side_effect.__str__()}
    assert not (world.pod_copy() / INDEX_DIR).exists(), "seeded index should be removed on failure"


def test_rerun_after_rebuild_does_not_publish_stale_seed(
    world: ActivatedWorld, artifacts: FakeArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-run while the reindex job is still in progress must not publish
    the golden-seeded (stale) index as the copy's recorded version."""
    manager = _legacy_copy(world, monkeypatch)
    store = DelegatedIndexStore(artifacts)
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: store)
    shutil.rmtree(world.pod_copy())  # pod B — no copy

    index_manager = MagicMock()
    index_manager.trigger_reindex.return_value = "job-rebuild"
    with world.forbid_local_io():
        first = _run(
            manager,
            artifacts,
            dry_run=False,
            rebuild_missing=True,
            index_manager=index_manager,
        )
    assert first.activated_rebuilt == {f"{USER}/{ALIAS}": "job-rebuild"}
    assert (world.pod_copy() / INDEX_DIR / BACKFILL_REBUILD_MARKER).exists()

    # Second run: the copy is "held here" (seed is present) and the job is
    # still active, so the marker prevents the premature fresh publish.
    active_im = MagicMock()
    active_im.background_job_manager.list_jobs.return_value = {
        "jobs": [{"operation_type": "reindex", "job_id": "job-rebuild"}]
    }
    second = _run(manager, artifacts, dry_run=False, index_manager=active_im)
    assert second.activated_published == []
    assert f"{USER}/{ALIAS}" in second.activated_rebuild_in_progress
    meta = manager.registration(USER, ALIAS)
    assert INDEX_VERSION_FIELD not in meta, "no version should be recorded yet"


def test_golden_already_queued_is_reported_not_failed(tmp_path: Path) -> None:
    """A DuplicateJobError from submit_job is reported as already_queued."""
    report = _backfill(
        _delegated(tmp_path),
        records=lambda: {"a-global": {"target_path": "/g/a"}},
        golden=lambda: {"a": "https://x/a.git"},
        refresh=MagicMock(side_effect=DuplicateJobError(
            "global_repo_refresh", "a", "job-42"
        )),
    ).run(submitter="admin", dry_run=False)
    assert "a-global" in report.golden_already_queued
    assert report.golden_failed == {}


def test_golden_already_queued_via_valueerror(tmp_path: Path) -> None:
    """A ValueError with the reindex-already-running message is also routed
    to already_queued (from trigger_reindex's per-user check)."""
    report = _backfill(
        _delegated(tmp_path),
        records=lambda: {"a-global": {"target_path": "/g/a"}},
        golden=lambda: {"a": "https://x/a.git"},
        refresh=MagicMock(side_effect=ValueError(
            "Another reindex job is already running/pending (job 42)"
        )),
    ).run(submitter="admin", dry_run=False)
    assert "a-global" in report.golden_already_queued
    assert report.golden_failed == {}


def test_stale_rebuild_marker_is_cleared_and_copy_published(
    world: ActivatedWorld, artifacts: FakeArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the marker exists but no reindex job is active, the marker is
    cleared and the copy is published (recovery from a failed job)."""
    manager = _legacy_copy(world, monkeypatch)
    store = DelegatedIndexStore(artifacts)
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: store)
    shutil.rmtree(world.pod_copy())  # pod B — no copy

    index_manager = MagicMock()
    index_manager.trigger_reindex.return_value = "job-rebuild"
    with world.forbid_local_io():
        _run(
            manager,
            artifacts,
            dry_run=False,
            rebuild_missing=True,
            index_manager=index_manager,
        )
    assert (world.pod_copy() / INDEX_DIR / BACKFILL_REBUILD_MARKER).exists()

    # Simulate: the job finished (or failed) — no active reindex jobs
    index_manager_empty = MagicMock()
    index_manager_empty.background_job_manager.list_jobs.return_value = {"jobs": []}
    report = _backfill(
        store,
        arm=manager,
        index_manager=index_manager_empty,
    ).run(submitter="admin", dry_run=False)
    assert report.activated_published == [f"{USER}/{ALIAS}"]
    assert report.activated_rebuild_in_progress == []
    assert not (world.pod_copy() / INDEX_DIR / BACKFILL_REBUILD_MARKER).exists()


def test_a_rebuild_seeds_from_the_served_golden_index(
    world: ActivatedWorld, artifacts: FakeArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.global_repos.alias_manager import AliasManager
    from code_indexer.utils.index_store import index_store_alias, published_index_fields

    manager = _legacy_copy(world, monkeypatch)
    store = DelegatedIndexStore(artifacts)
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: store)
    version = store.publish(world.pod_golden, world.pod_golden / INDEX_DIR)
    AliasManager(str(world.pod_data / "golden-repos" / "aliases")).create_alias(
        f"{ALIAS}-global",
        str(world.pod_golden),
        repo_name=ALIAS,
        extra=published_index_fields(index_store_alias(ALIAS), str(version)),
    )
    golden_config = (world.pod_golden / INDEX_DIR / "config.json").read_text()
    shutil.rmtree(world.pod_golden / INDEX_DIR)  # this pod never held golden
    shutil.rmtree(world.pod_copy())  # nor the copy
    manager.golden_repo_manager.get_actual_repo_path.side_effect = AssertionError(
        "seeded from the pod disk instead of the served golden index"
    )
    report = _run(manager, artifacts, dry_run=False, rebuild_missing=True)
    assert list(report.activated_rebuilt) == [f"{USER}/{ALIAS}"], report
    assert (world.pod_copy() / INDEX_DIR / "config.json").read_text() == golden_config
