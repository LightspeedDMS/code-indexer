"""No pointer write under a delegated IndexStore leaves another pod serving a
stale or missing index (review of !191).

* ``AliasManager.swap_alias`` without the published-index fields DROPS the old
  ones: they describe the old target, and keeping them made a pod that does
  not hold the new target check the OLD version out into it -- silently
  stale. With them dropped that pod fails loud.
* The fleet-migration snapshot (``trigger_post_consolidation_snapshot``) and
  the branch change (``GoldenRepoManager.change_branch``) publish their
  snapshot and record its version with the pointer, so any pod serves it.
* Pruning a superseded checkout goes through the ``CleanupManager`` (refcount,
  reader lease, minimum age), never a direct delete.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from code_indexer.config import ConfigManager
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services import index_materializer as im
from code_indexer.server.services.fleet_migration.snapshot_trigger import (
    trigger_post_consolidation_snapshot,
)
from code_indexer.server.startup import index_store_wiring
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.server.utils import registry_factory
from code_indexer.utils.index_store import (
    INDEX_DIRNAME,
    INDEX_STORE_ALIAS_FIELD,
    INDEX_VERSION_FIELD,
    DelegatedIndexStore,
    LocalIndexStore,
    publish_index_fields,
    published_index_fields,
)
from tests.unit.server.index_store_fakes import FakeArtifacts

REPO = "agent-router"
ALIAS = f"{REPO}-global"


def _build(dir_: Path, payload: bytes) -> Path:
    built = dir_ / INDEX_DIRNAME
    (built / "index").mkdir(parents=True)
    (built / "index" / "hnsw.bin").write_bytes(payload)
    (built / "config.json").write_text("{}")
    return built


def _hnsw(target: Path) -> bytes:
    return (target / INDEX_DIRNAME / "index" / "hnsw.bin").read_bytes()


@pytest.fixture
def artifacts(tmp_path: Path) -> FakeArtifacts:
    return FakeArtifacts(tmp_path / "store")


@pytest.fixture
def store(artifacts: FakeArtifacts) -> DelegatedIndexStore:
    return DelegatedIndexStore(artifacts)


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch, store: DelegatedIndexStore) -> List[Path]:
    """This process serves ``store``; fix-config is recorded, not run."""
    fixes: List[Path] = []
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: store)
    monkeypatch.setattr(im, "run_fix_config", lambda p: fixes.append(Path(p)))
    return fixes


# ── swap_alias: the published fields never outlive the target they describe ──


def _aliases(tmp_path: Path) -> AliasManager:
    return AliasManager(str(tmp_path / "golden-repos" / "aliases"))


def test_a_swap_without_published_fields_drops_the_old_ones(tmp_path: Path) -> None:
    aliases = _aliases(tmp_path)
    aliases.create_alias(
        ALIAS, "/g/.versioned/r/v_1", extra=published_index_fields(REPO, "v1")
    )
    aliases.swap_alias(ALIAS, "/g/.versioned/r/v_2", "/g/.versioned/r/v_1")
    record = aliases.read_record(ALIAS)
    assert record is not None
    assert record["target_path"] == "/g/.versioned/r/v_2"
    assert INDEX_STORE_ALIAS_FIELD not in record
    assert INDEX_VERSION_FIELD not in record


def test_a_swap_with_published_fields_records_the_new_ones(tmp_path: Path) -> None:
    aliases = _aliases(tmp_path)
    aliases.create_alias(
        ALIAS, "/g/.versioned/r/v_1", extra=published_index_fields(REPO, "v1")
    )
    aliases.swap_alias(
        ALIAS,
        "/g/.versioned/r/v_2",
        "/g/.versioned/r/v_1",
        extra={"other": "kept", **published_index_fields(REPO, "v2")},
    )
    record = aliases.read_record(ALIAS)
    assert record is not None
    assert record[INDEX_VERSION_FIELD] == "v2"
    assert record["other"] == "kept"


def test_a_swap_keeps_unrelated_fields(tmp_path: Path) -> None:
    aliases = _aliases(tmp_path)
    aliases.create_alias(ALIAS, "/a", extra={"meta_fingerprint": "fp"})
    aliases.swap_alias(ALIAS, "/b", "/a")
    record = aliases.read_record(ALIAS)
    assert record is not None and record["meta_fingerprint"] == "fp"


def test_an_unpublished_swap_fails_loud_on_a_pod_without_the_target(
    tmp_path: Path, store: DelegatedIndexStore, serve: List[Path]
) -> None:
    """The reviewed hazard: before the fix pod B checked v1 out into v_2."""
    scratch = tmp_path / "pod-a" / REPO
    v1 = store.publish(scratch, _build(scratch, b"old"))
    assert v1 is not None
    aliases = _aliases(tmp_path / "pod-b")
    old = str(tmp_path / "pod-b" / ".versioned" / REPO / "v_1")
    new = str(tmp_path / "pod-b" / ".versioned" / REPO / "v_2")
    aliases.create_alias(ALIAS, old, extra=published_index_fields(REPO, v1))
    aliases.swap_alias(ALIAS, new, old)
    with pytest.raises(im.PublishedIndexUnavailableError, match="refresh"):
        aliases.ensure_served_here(ALIAS, new)
    assert not Path(new).exists(), "checked the old version out into the new path"


# ── publish_index_fields ─────────────────────────────────────────────────────


def test_publish_index_fields_publishes_what_was_built_under_the_repos_alias(
    tmp_path: Path, store: DelegatedIndexStore, artifacts: FakeArtifacts
) -> None:
    snapshot = tmp_path / ".versioned" / REPO / "v_9"
    _build(snapshot, b"snap")
    fields = publish_index_fields(store, tmp_path / REPO, snapshot)
    assert fields == published_index_fields(REPO, str(artifacts.current_version(REPO)))
    assert artifacts.calls[-1][3] == str(snapshot / INDEX_DIRNAME)


def test_publish_index_fields_is_nothing_under_the_local_store(tmp_path: Path) -> None:
    assert publish_index_fields(LocalIndexStore(), tmp_path, tmp_path) is None


def test_publish_index_fields_refuses_a_store_that_answers_no_version(
    tmp_path: Path, artifacts: FakeArtifacts
) -> None:
    class NoVersion(DelegatedIndexStore):
        def publish(self, repo_path: Any, built_dir: Path) -> Any:
            return ""

    with pytest.raises(RuntimeError, match="returned no version"):
        publish_index_fields(NoVersion(artifacts), tmp_path / REPO, tmp_path)


# ── the fleet-migration snapshot publishes ───────────────────────────────────


def _scheduler(tmp_path: Path, index_store: Any) -> RefreshScheduler:
    golden = tmp_path / "golden-repos"
    golden.mkdir(parents=True, exist_ok=True)
    query_tracker = QueryTracker()
    return RefreshScheduler(
        golden_repos_dir=str(golden),
        config_source=ConfigManager(),
        query_tracker=query_tracker,
        cleanup_manager=CleanupManager(query_tracker),
        snapshot_manager=VersionedSnapshotManager(
            versioned_base=str(tmp_path / "versioned")
        ),
        index_store=index_store,
    )


@pytest.mark.parametrize("existing_alias", [False, True])
def test_the_migration_snapshot_is_served_on_a_pod_that_did_not_build_it(
    tmp_path: Path,
    store: DelegatedIndexStore,
    artifacts: FakeArtifacts,
    serve: List[Path],
    existing_alias: bool,
) -> None:
    scheduler = _scheduler(tmp_path, store)
    source = tmp_path / "golden-repos" / REPO
    _build(source, b"consolidated")
    if existing_alias:
        first = trigger_post_consolidation_snapshot(scheduler, REPO, str(source))
        assert first
    target = trigger_post_consolidation_snapshot(scheduler, REPO, str(source))

    record = scheduler.alias_manager.read_record(ALIAS)
    assert record is not None and record["target_path"] == target
    assert record[INDEX_VERSION_FIELD] == artifacts.current_version(REPO)
    assert record[INDEX_STORE_ALIAS_FIELD] == REPO
    published_from = [c[3] for c in artifacts.calls if c[0] == "publish"][-1]
    assert published_from == str(Path(target) / INDEX_DIRNAME)

    shutil.rmtree(target)  # pod B: shares the pointer, not pod A's disk
    scheduler.alias_manager.ensure_served_here(ALIAS, target)
    assert _hnsw(Path(target)) == b"consolidated"


def test_the_local_migration_snapshot_publishes_nothing(
    tmp_path: Path, artifacts: FakeArtifacts
) -> None:
    scheduler = _scheduler(tmp_path, LocalIndexStore())
    source = tmp_path / "golden-repos" / REPO
    _build(source, b"consolidated")
    trigger_post_consolidation_snapshot(scheduler, REPO, str(source))
    record = scheduler.alias_manager.read_record(ALIAS)
    assert record is not None and INDEX_VERSION_FIELD not in record
    assert artifacts.calls == []


def test_a_failed_migration_publish_moves_no_pointer(
    tmp_path: Path, store: DelegatedIndexStore, artifacts: FakeArtifacts
) -> None:
    scheduler = _scheduler(tmp_path, store)
    source = tmp_path / "golden-repos" / REPO
    _build(source, b"consolidated")
    artifacts.fail_publish = True
    with pytest.raises(OSError):
        trigger_post_consolidation_snapshot(scheduler, REPO, str(source))
    assert scheduler.alias_manager.read_record(ALIAS) is None


# ── the branch change publishes ──────────────────────────────────────────────


def _branch_change_manager(tmpdir: str, snapshot: Path, order: List[str]) -> Any:
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepo,
        GoldenRepoManager,
    )

    manager: Any = GoldenRepoManager(data_dir=tmpdir)
    manager.background_job_manager = MagicMock()
    manager.activated_repo_manager = MagicMock()
    created_at = datetime.now(timezone.utc).isoformat()
    clone_path = str(Path(tmpdir) / "golden-repos" / REPO)
    manager.golden_repos[REPO] = GoldenRepo(
        alias=REPO,
        repo_url="https://example.invalid/r.git",
        default_branch="main",
        clone_path=clone_path,
        created_at=created_at,
    )
    manager._cb_git_fetch_and_validate = MagicMock()
    manager._cb_checkout_and_pull = MagicMock()
    manager._cb_cidx_index = MagicMock()
    manager._cb_cow_snapshot = MagicMock(return_value=str(snapshot))
    manager._cb_fts_branch_cleanup = MagicMock()
    manager._cb_hnsw_branch_cleanup = MagicMock(
        side_effect=lambda *a: order.append("hnsw_cleanup")
    )
    manager._cb_swap_alias = MagicMock(side_effect=lambda *a: order.append("swap"))
    manager._sqlite_backend = MagicMock()
    manager._sqlite_backend.get_repo.return_value = {
        "alias": REPO,
        "repo_url": "https://example.invalid/r.git",
        "default_branch": "main",
        "clone_path": clone_path,
        "created_at": created_at,
        "enable_temporal": False,
        "temporal_options": None,
        "category_id": None,
        "category_auto_assigned": False,
    }
    manager.resource_config = None
    return manager


def test_a_branch_change_publishes_the_cleaned_snapshot_before_the_swap(
    store: DelegatedIndexStore,
    artifacts: FakeArtifacts,
    serve: List[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: List[str] = []
    real_publish = store.publish

    def publish(repo_path: Any, built_dir: Path) -> Any:
        order.append("publish")
        return real_publish(repo_path, built_dir)

    monkeypatch.setattr(store, "publish", publish)
    with tempfile.TemporaryDirectory() as tmpdir:
        snapshot = Path(tmpdir) / ".versioned" / REPO / "v_5"
        _build(snapshot, b"branch b")
        manager = _branch_change_manager(tmpdir, snapshot, order)
        manager.change_branch(alias=REPO, target_branch="b")

    assert order == ["hnsw_cleanup", "publish", "swap"]
    fields = published_index_fields(REPO, str(artifacts.current_version(REPO)))
    manager._cb_swap_alias.assert_called_once_with(REPO, str(snapshot), fields)
    assert artifacts.calls[-1][3] == str(snapshot / INDEX_DIRNAME)


def test_a_branch_change_swap_records_the_published_index(tmp_path: Path) -> None:
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoManager,
    )

    manager: Any = GoldenRepoManager.__new__(GoldenRepoManager)
    manager.golden_repos_dir = str(tmp_path)
    manager._snapshot_manager = None
    aliases = AliasManager(str(tmp_path / "aliases"))
    aliases.create_alias(ALIAS, str(tmp_path / REPO), extra={"index_version": "v1"})
    fields: Dict[str, str] = published_index_fields(REPO, "v2")
    manager._cb_swap_alias(REPO, str(tmp_path / ".versioned" / REPO / "v_2"), fields)
    record = aliases.read_record(ALIAS)
    assert record is not None
    assert record[INDEX_VERSION_FIELD] == "v2"
    assert record[INDEX_STORE_ALIAS_FIELD] == REPO


# ── pruning goes through the CleanupManager ──────────────────────────────────


class _Lifecycle:
    def __init__(self, cleanup_manager: Any) -> None:
        self.cleanup_manager = cleanup_manager


class _State:
    def __init__(self, cleanup_manager: Any) -> None:
        self.global_lifecycle_manager = _Lifecycle(cleanup_manager)


def _superseded(tmp_path: Path) -> Dict[str, Path]:
    ns = tmp_path / "golden-repos" / ".versioned" / REPO
    old = ns / "v_1750000000"
    _build(old, b"old")
    (old / im.CHECKED_OUT_MARKER).write_text("v")
    return {"target": ns / "v_1760000000", "old": old}


def test_a_superseded_checkout_is_scheduled_never_deleted_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _superseded(tmp_path)
    cleanup_manager = CleanupManager(QueryTracker())
    monkeypatch.setattr(
        registry_factory,
        "_running_server_app_state",
        lambda: _State(cleanup_manager),
    )
    im._prune_superseded(paths["target"], {})
    assert cleanup_manager.get_pending_cleanups() == {str(paths["old"])}
    assert paths["old"].exists(), "deleted under a possibly running query"


def test_without_a_cleanup_manager_nothing_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _superseded(tmp_path)
    monkeypatch.setattr(registry_factory, "_running_server_app_state", lambda: None)
    im._prune_superseded(paths["target"], {})
    assert paths["old"].exists()


def test_a_failing_cleanup_manager_never_fails_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _superseded(tmp_path)
    broken = MagicMock()
    broken.schedule_cleanup.side_effect = OSError("db down")
    monkeypatch.setattr(
        registry_factory, "_running_server_app_state", lambda: _State(broken)
    )
    im._prune_superseded(paths["target"], {})
    broken.schedule_cleanup.assert_called_once_with(str(paths["old"]))
    assert paths["old"].exists()
