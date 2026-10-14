"""An activated copy's index is served on ANY pod (IndexStore, delegated).

Pod A activates (or reindexes) a copy and publishes its index; the registry
entry records ``index_store_alias`` / ``index_version``. Pod B -- whose copy
directory does not exist -- serves the copy from the store. Run over the
``ActivatedWorld`` fakes (workspace-mcp + git-mcp + a real-tree artifact
service), with every local access to a pod repository path forbidden:

* activation publishes and records the version; a failed publish leaves no
  copy and no registration;
* pod B checks the recorded version out; a pod holding an older version
  swaps it for the recorded one; the publisher fetches nothing;
* a reindex/branch delta publishes on top of the version it was built from
  (another pod's publish in between is refused);
* deactivation deletes the alias from the store; a failed delete keeps the
  registration;
* the registry gates every access (another user / unknown alias: nothing is
  fetched), and an entry naming a foreign alias is refused;
* activated aliases never collide with golden ones;
* local IndexStore: today's activation document, no store, no extra reads.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
    ActivatedRepoManager,
)
from code_indexer.server.services import index_materializer as im
from code_indexer.server.services.activated_index_store import (
    ActivatedIndexStore,
    serve_activated_index,
)
from code_indexer.server.startup import index_store_wiring
from code_indexer.utils.index_store import (
    ACTIVATED_STORE_ALIAS_PREFIX,
    INDEX_STORE_ALIAS_FIELD,
    INDEX_VERSION_FIELD,
    DelegatedIndexStore,
    LocalIndexStore,
    activated_index_store_alias,
    index_store_alias,
    is_activated_store_alias,
    published_index_fields,
)
from tests.unit.server.activated_seam_fakes import (
    ALIAS,
    INDEX_DIR,
    USER,
    ActivatedWorld,
)
from tests.unit.server.index_store_fakes import FakeArtifacts

COPY_ALIAS = activated_index_store_alias(USER, ALIAS)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ActivatedWorld:
    return ActivatedWorld(tmp_path, monkeypatch)


@pytest.fixture
def artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeArtifacts:
    fake = FakeArtifacts(tmp_path / "store")
    store = DelegatedIndexStore(fake)
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: store)
    monkeypatch.setattr(im, "run_fix_config", lambda target: None)
    return fake


def _manager(world: ActivatedWorld) -> ActivatedRepoManager:
    manager = world.delegated_manager()
    manager.golden_repo_manager.golden_repos_dir = str(world.pod_data / "golden-repos")
    return manager


def _meta(manager: ActivatedRepoManager) -> Dict[str, Any]:
    """The copy's metadata, which these tests require to exist."""
    meta = manager._load_metadata(USER, ALIAS)
    assert meta is not None, "the activated copy has no metadata"
    return meta


def _activate(manager: ActivatedRepoManager, user_alias: str = ALIAS) -> Any:
    return manager._do_activate_repository(
        username=USER,
        golden_repo_alias=ALIAS,
        branch_name="main",
        user_alias=user_alias,
    )


def _index_bytes(copy: Path) -> Dict[str, bytes]:
    root = copy / INDEX_DIR
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _forget_pod(world: ActivatedWorld) -> None:
    """Pod B: nothing of the copy on this pod."""
    shutil.rmtree(world.pod_copy())


# ── activation publishes ────────────────────────────────────────────────────


def test_activation_publishes_and_records_the_version(
    world: ActivatedWorld, artifacts: FakeArtifacts
) -> None:
    manager = _manager(world)
    with world.forbid_local_io():
        _activate(manager)
    meta = _meta(manager)
    assert meta[INDEX_STORE_ALIAS_FIELD] == COPY_ALIAS
    assert meta[INDEX_VERSION_FIELD] == artifacts.current_version(COPY_ALIAS)
    published = artifacts.root / COPY_ALIAS / meta[INDEX_VERSION_FIELD]
    assert _index_bytes(world.pod_copy()) == {
        str(p.relative_to(published)): p.read_bytes()
        for p in sorted(published.rglob("*"))
        if p.is_file()
    }


def test_a_failed_publish_fails_the_activation_leaving_nothing(
    world: ActivatedWorld, artifacts: FakeArtifacts
) -> None:
    manager = _manager(world)
    artifacts.fail_publish = True
    with pytest.raises(ActivatedRepoError, match="publish"):
        with world.forbid_local_io():
            _activate(manager)
    assert manager._load_metadata(USER, ALIAS) is None
    assert not world.remote_copy().exists()
    assert not (world.pod_copy() / INDEX_DIR).exists()


def test_activation_on_a_pod_without_the_golden_index_checks_it_out(
    world: ActivatedWorld, artifacts: FakeArtifacts
) -> None:
    store = DelegatedIndexStore(artifacts)
    version = store.publish(world.pod_golden, world.pod_golden / INDEX_DIR)
    AliasManager(str(world.pod_data / "golden-repos" / "aliases")).create_alias(
        f"{ALIAS}-global",
        str(world.pod_golden),
        repo_name=ALIAS,
        extra=published_index_fields(index_store_alias(ALIAS), str(version)),
    )
    golden_index = _index_bytes(world.pod_golden)
    shutil.rmtree(world.pod_golden / INDEX_DIR)  # this pod never built it
    manager = _manager(world)
    with world.forbid_local_io():
        _activate(manager)
    assert _index_bytes(world.pod_golden) == golden_index
    copy_index = _index_bytes(world.pod_copy())
    assert copy_index == golden_index


# ── serving on another pod ──────────────────────────────────────────────────


def test_pod_b_serves_the_recorded_version(
    world: ActivatedWorld, artifacts: FakeArtifacts
) -> None:
    manager = _manager(world)
    _activate(manager)
    expected = _index_bytes(world.pod_copy())
    _forget_pod(world)
    with world.forbid_local_io():
        path = manager.serve_index_here(USER, ALIAS)
    assert path == str(world.pod_copy())
    assert _index_bytes(world.pod_copy()) == expected


def test_the_publisher_fetches_nothing(
    world: ActivatedWorld, artifacts: FakeArtifacts
) -> None:
    manager = _manager(world)
    _activate(manager)
    artifacts.calls.clear()
    manager.serve_index_here(USER, ALIAS)
    assert artifacts.calls == []


def test_an_older_checkout_is_swapped_for_the_recorded_version(
    world: ActivatedWorld, artifacts: FakeArtifacts
) -> None:
    manager = _manager(world)
    _activate(manager)
    old = _index_bytes(world.pod_copy())
    # Another pod reindexed: v2 published and recorded.
    other = world.tmp / "pod-c" / ALIAS
    shutil.copytree(world.pod_copy() / INDEX_DIR, other / INDEX_DIR)
    (other / INDEX_DIR / "index" / "collection" / "chunks.db").write_bytes(b"v2")
    meta = _meta(manager)
    meta.update(ActivatedIndexStore().publish(USER, ALIAS, other, meta))
    manager._save_metadata(USER, ALIAS, meta)
    assert _index_bytes(world.pod_copy()) == old
    with world.forbid_local_io():
        manager.serve_index_here(USER, ALIAS)
    assert (
        world.pod_copy() / INDEX_DIR / "index" / "collection" / "chunks.db"
    ).read_bytes() == b"v2"
    assert not (world.pod_copy() / im.RETIRED_DIRNAME).exists()
    assert not (world.pod_copy() / im.INCOMING_DIRNAME).exists()


def test_a_failed_swap_keeps_the_older_index(
    world: ActivatedWorld, artifacts: FakeArtifacts
) -> None:
    manager = _manager(world)
    _activate(manager)
    old = _index_bytes(world.pod_copy())
    meta = _meta(manager)
    meta.update(ActivatedIndexStore().publish(USER, ALIAS, world.pod_copy(), meta))
    manager._save_metadata(USER, ALIAS, meta)
    (world.pod_copy() / im.CHECKED_OUT_MARKER).write_text("an older version")
    artifacts.fail_checkout = True
    with pytest.raises(im.PublishedIndexUnavailableError):
        manager.serve_index_here(USER, ALIAS)
    assert _index_bytes(world.pod_copy()) == old


def test_a_failure_after_the_swap_restores_the_older_index(
    world: ActivatedWorld, artifacts: FakeArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(world)
    _activate(manager)
    old = _index_bytes(world.pod_copy())
    other = world.tmp / "pod-c" / ALIAS
    shutil.copytree(world.pod_copy() / INDEX_DIR, other / INDEX_DIR)
    (other / INDEX_DIR / "index" / "collection" / "chunks.db").write_bytes(b"v2")
    meta = _meta(manager)
    meta.update(ActivatedIndexStore().publish(USER, ALIAS, other, meta))
    manager._save_metadata(USER, ALIAS, meta)

    def broken_fix(target: Path) -> None:
        raise RuntimeError("fix-config failed")

    monkeypatch.setattr(im, "run_fix_config", broken_fix)
    with pytest.raises(im.PublishedIndexUnavailableError, match="fix-config"):
        manager.serve_index_here(USER, ALIAS)
    assert _index_bytes(world.pod_copy()) == old
    assert not (world.pod_copy() / im.IN_PROGRESS_MARKER).exists()


def test_an_unreachable_store_is_an_error_not_a_missing_repo(
    world: ActivatedWorld, artifacts: FakeArtifacts
) -> None:
    manager = _manager(world)
    _activate(manager)
    _forget_pod(world)
    artifacts.fail_checkout = True
    with pytest.raises(im.PublishedIndexUnavailableError):
        manager.serve_index_here(USER, ALIAS)


def test_an_activation_from_before_publishing_is_served_only_where_it_is(
    world: ActivatedWorld, artifacts: FakeArtifacts
) -> None:
    manager = _manager(world)
    _activate(manager)
    meta = _meta(manager)
    for field in (INDEX_STORE_ALIAS_FIELD, INDEX_VERSION_FIELD):
        meta.pop(field)
    manager._save_metadata(USER, ALIAS, meta)
    manager.serve_index_here(USER, ALIAS)  # this pod holds it
    _forget_pod(world)
    with pytest.raises(im.PublishedIndexUnavailableError, match="no published"):
        manager.serve_index_here(USER, ALIAS)


# ── publishing after a reindex ──────────────────────────────────────────────


def test_a_reindex_publishes_on_top_of_the_version_it_was_built_from(
    world: ActivatedWorld, artifacts: FakeArtifacts
) -> None:
    manager = _manager(world)
    _activate(manager)
    first = _meta(manager)[INDEX_VERSION_FIELD]
    (world.pod_copy() / INDEX_DIR / "index" / "collection" / "chunks.db").write_bytes(
        b"re"
    )
    with world.forbid_local_io():
        manager.publish_index(USER, ALIAS)
    second = _meta(manager)[INDEX_VERSION_FIELD]
    assert second != first and second == artifacts.current_version(COPY_ALIAS)
    assert (
        "publish",
        COPY_ALIAS,
        first,
        str(world.pod_copy() / INDEX_DIR),
    ) in artifacts.calls
    assert (world.pod_copy() / im.CHECKED_OUT_MARKER).read_text() == second


def test_a_publish_by_another_pod_in_between_is_refused(
    world: ActivatedWorld, artifacts: FakeArtifacts
) -> None:
    manager = _manager(world)
    _activate(manager)
    DelegatedIndexStore(artifacts).publish_as(COPY_ALIAS, world.pod_copy() / INDEX_DIR)
    with pytest.raises(RuntimeError, match="ECONFLICT"):
        manager.publish_index(USER, ALIAS)


def test_a_branch_delta_serves_first_and_publishes_after(
    world: ActivatedWorld, artifacts: FakeArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(world)
    _activate(manager)
    order = []

    class IndexManager:
        def run_branch_delta_index(
            self, repo_path: str, cancel_check: Any = None
        ) -> None:
            order.append("index")

    manager._index_manager = IndexManager()

    def serve(username: str, user_alias: str) -> str:
        order.append("serve")
        return ""

    monkeypatch.setattr(manager, "serve_index_here", serve)
    monkeypatch.setattr(manager, "publish_index", lambda u, a: order.append("publish"))
    manager._run_branch_delta_index(
        str(world.pod_copy()), ALIAS, **manager._publishing_owner(USER)
    )
    assert order == ["serve", "index", "publish"]


# ── deactivation ────────────────────────────────────────────────────────────


def test_deactivation_deletes_the_published_index(
    world: ActivatedWorld, artifacts: FakeArtifacts
) -> None:
    manager = _manager(world)
    _activate(manager)
    with world.forbid_local_io():
        manager._do_deactivate_repository(USER, ALIAS)
    assert ("delete_alias", COPY_ALIAS) in artifacts.calls
    assert artifacts.current_version(COPY_ALIAS) is None
    assert manager._load_metadata(USER, ALIAS) is None


def test_a_failed_index_delete_keeps_the_registration(
    world: ActivatedWorld, artifacts: FakeArtifacts
) -> None:
    manager = _manager(world)
    _activate(manager)
    artifacts.fail_delete = True
    with pytest.raises(Exception, match="unreachable"):
        manager._do_deactivate_repository(USER, ALIAS)
    assert manager._load_metadata(USER, ALIAS) is not None
    artifacts.fail_delete = False
    manager._do_deactivate_repository(USER, ALIAS)  # finishing the job
    assert manager._load_metadata(USER, ALIAS) is None
    assert artifacts.current_version(COPY_ALIAS) is None


# ── isolation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("who", [("bob", ALIAS), (USER, "not-activated")])
def test_the_registry_gates_every_fetch(
    world: ActivatedWorld, artifacts: FakeArtifacts, who: Any
) -> None:
    manager = _manager(world)
    _activate(manager)
    artifacts.calls.clear()
    with pytest.raises(ActivatedRepoError, match="not found"):
        manager.serve_index_here(*who)
    with pytest.raises(ActivatedRepoError, match="not found"):
        manager.publish_index(*who)
    assert artifacts.calls == []


def test_an_entry_naming_another_alias_is_refused(
    world: ActivatedWorld, artifacts: FakeArtifacts
) -> None:
    manager = _manager(world)
    _activate(manager)
    meta = _meta(manager)
    meta[INDEX_STORE_ALIAS_FIELD] = activated_index_store_alias("bob", ALIAS)
    manager._save_metadata(USER, ALIAS, meta)
    _forget_pod(world)
    artifacts.calls.clear()
    with pytest.raises(im.PublishedIndexUnavailableError, match="refusing"):
        manager.serve_index_here(USER, ALIAS)
    assert artifacts.calls == []


@pytest.mark.parametrize(
    "golden",
    [
        "activated.0123",
        ACTIVATED_STORE_ALIAS_PREFIX + "a" * 40,
        "Activated.X",
        "activated",
    ],
)
def test_golden_aliases_never_enter_the_activated_namespace(golden: str) -> None:
    alias = index_store_alias(golden)
    assert not alias.startswith(ACTIVATED_STORE_ALIAS_PREFIX)
    assert not is_activated_store_alias(alias)


def test_activated_aliases_are_per_user_and_copy() -> None:
    names = {
        activated_index_store_alias(u, a)
        for u in ("alice", "bob", "alice\0x")
        for a in ("repo", "repo2", "x")
    }
    assert len(names) == 9
    assert all(is_activated_store_alias(n) for n in names)
    assert activated_index_store_alias("alice", "repo") == activated_index_store_alias(
        "alice", "repo"
    )
    with pytest.raises(ValueError):
        activated_index_store_alias("", "repo")
    with pytest.raises(ValueError):
        activated_index_store_alias("alice", "")


# ── local IndexStore: today's behaviour ─────────────────────────────────────


def test_local_activation_writes_todays_document(
    world: ActivatedWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        index_store_wiring, "server_index_store", lambda: LocalIndexStore()
    )
    manager = _manager(world)
    _activate(manager)
    meta = _meta(manager)
    assert INDEX_STORE_ALIAS_FIELD not in meta and INDEX_VERSION_FIELD not in meta
    assert sorted(p.name for p in world.pod_copy().iterdir()) == [INDEX_DIR]
    assert manager._publishing_owner(USER) == {}


def test_local_serving_reads_nothing(
    world: ActivatedWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        index_store_wiring, "server_index_store", lambda: LocalIndexStore()
    )
    manager = world.local_manager()
    monkeypatch.setattr(
        manager, "_load_metadata", lambda *a: pytest.fail("registry read locally")
    )
    assert manager.serve_index_here(USER, "never-activated") == str(
        world.local_copy("never-activated")
    )
    manager.publish_index(USER, "never-activated")
    ActivatedIndexStore().delete(USER, "never-activated")


def test_serve_activated_index_is_inert_locally_and_gated_when_delegated() -> None:
    calls = []

    class Manager:
        def serve_index_here(self, username: str, user_alias: str) -> str:
            calls.append((username, user_alias))
            return ""

    serve_activated_index(Manager(), USER, ALIAS, store_provider=LocalIndexStore)
    assert calls == []
    serve_activated_index(
        Manager(),
        USER,
        ALIAS,
        store_provider=lambda: DelegatedIndexStore(FakeArtifacts(Path("/nonexistent"))),
    )
    assert calls == [(USER, ALIAS)]


def test_activated_aliases_do_not_run_user_and_alias_together() -> None:
    assert activated_index_store_alias("ab", "c") != activated_index_store_alias(
        "a", "bc"
    )


def test_deleting_needs_a_client_that_can_delete() -> None:
    class Old:
        def current_version(self, alias: str) -> None:
            return None

    with pytest.raises(RuntimeError, match="0.9.0"):
        DelegatedIndexStore(Old()).delete_as(COPY_ALIAS)


def test_an_activation_over_a_leftover_published_index_replaces_it(
    world: ActivatedWorld, artifacts: FakeArtifacts
) -> None:
    """A crash after publishing but before registering leaves a version
    behind; the next activation of the same copy publishes over it."""
    leftover = world.tmp / "leftover"
    (leftover / INDEX_DIR / "index").mkdir(parents=True)
    DelegatedIndexStore(artifacts).publish_as(COPY_ALIAS, leftover / INDEX_DIR)
    manager = _manager(world)
    _activate(manager)
    meta = _meta(manager)
    assert meta[INDEX_VERSION_FIELD] == artifacts.current_version(COPY_ALIAS)
    assert len(artifacts.versions[COPY_ALIAS]) == 2


# ── CAS base from marker, registry save tolerance ─────────────────────────


def test_cas_base_from_marker_not_registry(
    world: ActivatedWorld, artifacts: FakeArtifacts
) -> None:
    """Another pod publishes v2 AND updates the registry while this pod's
    CHECKED_OUT_MARKER still holds v1; publish from this pod uses v1 as
    the CAS base (-> ECONFLICT because the store's current is v2)."""
    manager = _manager(world)
    _activate(manager)
    v1 = _meta(manager)[INDEX_VERSION_FIELD]

    # Another pod publishes v2 and updates the registry.
    other = world.tmp / "pod-c" / ALIAS
    shutil.copytree(world.pod_copy() / INDEX_DIR, other / INDEX_DIR)
    (other / INDEX_DIR / "index" / "collection" / "chunks.db").write_bytes(b"v2")
    meta = _meta(manager)
    meta.update(ActivatedIndexStore().publish(USER, ALIAS, other, meta))
    manager._save_metadata(USER, ALIAS, meta)
    v2 = _meta(manager)[INDEX_VERSION_FIELD]
    assert v2 != v1

    # This pod's marker still says v1.
    assert (world.pod_copy() / im.CHECKED_OUT_MARKER).read_text() == v1

    # publish_index reads the marker (v1), not the registry (v2), so
    # the CAS base is v1, which conflicts with the store's current (v2).
    with pytest.raises(RuntimeError, match="ECONFLICT"):
        manager.publish_index(USER, ALIAS)

    # The publish call in artifacts confirms v1 was used as base.
    publish_calls = [c for c in artifacts.calls if c[0] == "publish"]
    last_publish = publish_calls[-1]
    assert last_publish[2] == v1  # base_version arg


def test_metadata_save_retries_once_then_serve_and_publish_work(
    world: ActivatedWorld,
    artifacts: FakeArtifacts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_save_metadata fails on the first attempt; the built-in retry saves
    the registry.  After that, serve_index_here + another publish works:
    the ECONFLICT loop the review flagged does not occur."""
    manager = _manager(world)
    _activate(manager)
    v1 = _meta(manager)[INDEX_VERSION_FIELD]

    # Mutate so the publish creates a distinct version.
    (world.pod_copy() / INDEX_DIR / "index" / "collection" / "chunks.db").write_bytes(
        b"re-1"
    )

    # Make _save_metadata fail once (retry succeeds).
    real_save = manager._save_metadata
    call_count = 0

    def flaky_save(*args: Any, **kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OSError("disk full")
        return real_save(*args, **kwargs)

    monkeypatch.setattr(manager, "_save_metadata", flaky_save)

    # First publish: first save attempt fails, retry succeeds.
    with world.forbid_local_io():
        manager.publish_index(USER, ALIAS)

    v2 = artifacts.current_version(COPY_ALIAS)
    assert v2 is not None and v2 != v1
    assert (world.pod_copy() / im.CHECKED_OUT_MARKER).read_text() == v2
    # Registry is consistent (retry succeeded).
    assert _meta(manager)[INDEX_VERSION_FIELD] == v2

    # serve_index_here sees marker v2 == registry v2, no swap.
    with world.forbid_local_io():
        manager.serve_index_here(USER, ALIAS)

    # Second publish after serve_index_here: no ECONFLICT.
    (world.pod_copy() / INDEX_DIR / "index" / "collection" / "chunks.db").write_bytes(
        b"re-2"
    )
    with world.forbid_local_io():
        manager.publish_index(USER, ALIAS)

    v3 = artifacts.current_version(COPY_ALIAS)
    assert v3 is not None and v3 != v2
    assert _meta(manager)[INDEX_VERSION_FIELD] == v3


def test_metadata_save_permanent_failure_raises(
    world: ActivatedWorld,
    artifacts: FakeArtifacts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _save_metadata fails on both attempts, publish_index raises
    (fails loud) instead of silently swallowing."""
    manager = _manager(world)
    _activate(manager)

    (world.pod_copy() / INDEX_DIR / "index" / "collection" / "chunks.db").write_bytes(
        b"re-1"
    )

    monkeypatch.setattr(
        manager,
        "_save_metadata",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        with world.forbid_local_io():
            manager.publish_index(USER, ALIAS)
