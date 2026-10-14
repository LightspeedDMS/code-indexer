"""A composite activated copy is served on ANY pod (IndexStore, delegated).

A composite's pod state is its proxy config (``<composite>/.code-indexer``)
plus each member's index (``<composite>/<member>/.code-indexer``). Activation
publishes all of them (the members under their own per-user, per-copy store
aliases) and records the versions in the registry entry; a pod that holds
nothing checks them all out before a query, so the composite is routed as a
composite -- not as one repository -- with every member's index present.
Deactivation deletes every alias. Over ``CompositeWorld`` with the
forbid-local-IO guard; the pod's composite directory does not exist on pod B.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.query.semantic_query_manager import SemanticQueryManager
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
)
from code_indexer.server.services import index_materializer as im
from code_indexer.server.services.activated_index_store import (
    MEMBER_INDEX_VERSIONS_FIELD,
)
from code_indexer.server.startup import index_store_wiring
from code_indexer.utils.index_store import (
    INDEX_VERSION_FIELD,
    DelegatedIndexStore,
    LocalIndexStore,
    activated_index_store_alias,
)
from tests.unit.server.activated_seam_fakes import INDEX_DIR, USER
from tests.unit.server.index_store_fakes import FakeArtifacts
from tests.unit.server.repositories.test_activated_composite_content_seam import (
    COMPOSITE,
    MEMBERS,
    CompositeWorld,
    _activate,
    _pod_composite,
)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CompositeWorld:
    return CompositeWorld(tmp_path, monkeypatch)


@pytest.fixture
def artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeArtifacts:
    fake = FakeArtifacts(tmp_path / "store")
    store = DelegatedIndexStore(fake)
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: store)
    monkeypatch.setattr(im, "run_fix_config", lambda target: None)
    return fake


def _manager(world: CompositeWorld) -> Any:
    manager = world.delegated_manager()
    manager.golden_repo_manager.golden_repos_dir = str(world.pod_data / "golden-repos")
    return manager


def _member_alias(member: str) -> str:
    return activated_index_store_alias(USER, f"{COMPOSITE}/{member}")


def _pod_state(world: CompositeWorld) -> Dict[str, bytes]:
    root = _pod_composite(world)
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and INDEX_DIR in p.relative_to(root).parts
    }


def test_composite_activation_publishes_config_and_members(
    world: CompositeWorld, artifacts: FakeArtifacts
) -> None:
    manager = _manager(world)
    with world.forbid_local_io():
        _activate(manager)
    meta = manager._load_metadata(USER, COMPOSITE)
    assert meta[INDEX_VERSION_FIELD] == artifacts.current_version(
        activated_index_store_alias(USER, COMPOSITE)
    )
    assert meta[MEMBER_INDEX_VERSIONS_FIELD] == {
        m: artifacts.current_version(_member_alias(m)) for m in MEMBERS
    }


def test_a_failed_member_publish_leaves_no_composite(
    world: CompositeWorld, artifacts: FakeArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = artifacts.publish
    calls: List[str] = []

    def publish(alias: str, base: Any, source: str) -> str:
        calls.append(alias)
        if len(calls) == 2:  # the composite's own config went; a member fails
            raise OSError("artifact service unreachable")
        return real(alias, base, source)

    monkeypatch.setattr(artifacts, "publish", publish)
    manager = _manager(world)
    with pytest.raises(ActivatedRepoError):
        _activate(manager)
    assert manager._load_metadata(USER, COMPOSITE) is None
    assert not world.remote_copy(COMPOSITE).exists()


def test_pod_b_queries_the_composite_as_a_composite(
    world: CompositeWorld, artifacts: FakeArtifacts, tmp_path: Path
) -> None:
    manager = _manager(world)
    _activate(manager)
    expected = _pod_state(world)
    shutil.rmtree(_pod_composite(world))  # pod B holds nothing of it

    query = SemanticQueryManager(
        data_dir=str(world.pod_data),
        activated_repo_manager=manager,
        background_job_manager=MagicMock(),
    )
    routed: List[Any] = []

    def cli_query(repo_path: Any, **kwargs: Any) -> List[Any]:
        routed.append(repo_path)
        return []

    # Everything up to the composite's CLI query runs for real; the CLI
    # query is reached only for a repository detected as a composite.
    with patch.object(query, "_execute_cli_query", side_effect=cli_query):
        with world.forbid_local_io():
            query._perform_search(
                username=USER,
                user_repos=[{"user_alias": COMPOSITE}],
                query_text="q",
                limit=5,
                min_score=None,
                file_extensions=None,
            )
    assert [str(p) for p in routed] == [str(_pod_composite(world))]
    assert _pod_state(world) == expected
    for member in MEMBERS:
        assert (_pod_composite(world) / member / INDEX_DIR / "config.json").is_file()


def test_a_member_with_no_recorded_version_fails_loud_on_pod_b(
    world: CompositeWorld, artifacts: FakeArtifacts
) -> None:
    manager = _manager(world)
    _activate(manager)
    meta = manager._load_metadata(USER, COMPOSITE)
    meta[MEMBER_INDEX_VERSIONS_FIELD].pop(MEMBERS[0])
    manager._save_metadata(USER, COMPOSITE, meta)
    shutil.rmtree(_pod_composite(world))
    with pytest.raises(im.PublishedIndexUnavailableError, match="no published"):
        manager.serve_index_here(USER, COMPOSITE)


def test_unreadable_member_versions_are_refused(
    world: CompositeWorld, artifacts: FakeArtifacts
) -> None:
    manager = _manager(world)
    _activate(manager)
    meta = manager._load_metadata(USER, COMPOSITE)
    meta[MEMBER_INDEX_VERSIONS_FIELD] = ["not", "a", "map"]
    manager._save_metadata(USER, COMPOSITE, meta)
    with pytest.raises(im.PublishedIndexUnavailableError, match="unreadable"):
        manager.serve_index_here(USER, COMPOSITE)


def test_a_composite_reindex_publishes_every_part_on_its_recorded_version(
    world: CompositeWorld, artifacts: FakeArtifacts
) -> None:
    manager = _manager(world)
    _activate(manager)
    before = manager._load_metadata(USER, COMPOSITE)
    manager.publish_index(USER, COMPOSITE)
    after = manager._load_metadata(USER, COMPOSITE)
    publishes = [c for c in artifacts.calls if c[0] == "publish"]
    for member in MEMBERS:
        assert (
            "publish",
            _member_alias(member),
            before[MEMBER_INDEX_VERSIONS_FIELD][member],
            str(_pod_composite(world) / member / INDEX_DIR),
        ) in publishes
        assert (
            after[MEMBER_INDEX_VERSIONS_FIELD][member]
            != before[MEMBER_INDEX_VERSIONS_FIELD][member]
        )


def test_deactivating_a_composite_deletes_every_alias(
    world: CompositeWorld, artifacts: FakeArtifacts
) -> None:
    manager = _manager(world)
    _activate(manager)
    manager._do_deactivate_repository(USER, COMPOSITE)
    deleted = {c[1] for c in artifacts.calls if c[0] == "delete_alias"}
    assert deleted == {activated_index_store_alias(USER, COMPOSITE)} | {
        _member_alias(m) for m in MEMBERS
    }
    assert all(artifacts.current_version(alias) is None for alias in deleted)


def test_member_aliases_are_distinct_from_single_copies() -> None:
    names = (
        {_member_alias(m) for m in MEMBERS}
        | {activated_index_store_alias(USER, COMPOSITE)}
        | {activated_index_store_alias(USER, m) for m in MEMBERS}
    )
    assert len(names) == 2 * len(MEMBERS) + 1


def test_partial_publish_retry_adopts_prior_version(
    world: CompositeWorld, artifacts: FakeArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config + member 1 publish, member 2 fails (OSError: service down). On
    retry the config's and member 1's CAS conflicts are resolved by adopting
    the versions this pod already published (their CHECKED_OUT_MARKERs match
    the store's current versions), and member 2 publishes normally."""
    manager = _manager(world)
    _activate(manager)  # seed: all members published

    # Simulate a reindex where the last member hits a connectivity error.
    # OSError is NOT caught by _publish_or_adopt (only RuntimeError /
    # PublishedIndexUnavailableError are CAS-eligible), so it propagates.
    real = artifacts.publish
    attempt = [0]

    def fail_last_member(alias: str, base: Any, source: str) -> str:
        attempt[0] += 1
        # Call 1 = composite config, 2 = member 1, 3 = member 2 (fail).
        if attempt[0] == 3:
            raise OSError("artifact service unreachable")
        return real(alias, base, source)

    monkeypatch.setattr(artifacts, "publish", fail_last_member)
    with pytest.raises(OSError, match="artifact service unreachable"):
        manager.publish_index(USER, COMPOSITE)

    # Retry: the registry still records the activation versions, but the
    # store advanced config + member 1 to N+1.  _publish_or_adopt must
    # detect each CAS conflict (RuntimeError from compare-and-swap) and
    # adopt the already-published version; member 2 publishes normally
    # because its store version never advanced.
    monkeypatch.setattr(artifacts, "publish", real)
    manager.publish_index(USER, COMPOSITE)
    meta = manager._load_metadata(USER, COMPOSITE)
    for member in MEMBERS:
        version = meta[MEMBER_INDEX_VERSIONS_FIELD][member]
        assert version == artifacts.current_version(_member_alias(member))


def test_partial_publish_retry_republishes_rebuilt_content(
    world: CompositeWorld, artifacts: FakeArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a reindex rebuilds member 1's index between the failed and retried
    publish, the retry must re-upload the new content (not silently adopt the
    store's stale version)."""
    manager = _manager(world)
    _activate(manager)

    real = artifacts.publish
    attempt = [0]

    def fail_last_member(alias: str, base: Any, source: str) -> str:
        attempt[0] += 1
        if attempt[0] == 3:
            raise OSError("artifact service unreachable")
        return real(alias, base, source)

    monkeypatch.setattr(artifacts, "publish", fail_last_member)
    with pytest.raises(OSError, match="artifact service unreachable"):
        manager.publish_index(USER, COMPOSITE)

    # Simulate a reindex that changes member 1's index content on disk.
    member_1 = MEMBERS[0]
    member_1_index = _pod_composite(world) / member_1 / INDEX_DIR
    sentinel = member_1_index / "rebuilt.marker"
    sentinel.write_text("rebuilt-after-failure")

    # Retry: _publish_or_adopt re-publishes member 1 on top of the current
    # version instead of adopting, so the rebuilt content is uploaded.
    monkeypatch.setattr(artifacts, "publish", real)
    manager.publish_index(USER, COMPOSITE)

    # The store must hold the rebuilt content (sentinel present).
    member_alias = _member_alias(member_1)
    version = artifacts.current_version(member_alias)
    assert version is not None
    published = artifacts.root / member_alias / version
    assert (published / "rebuilt.marker").exists(), (
        "store must contain the rebuilt content, not the pre-failure version"
    )


def test_transient_error_on_same_base_version_propagates(
    world: CompositeWorld, artifacts: FakeArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RuntimeError that is NOT a CAS conflict (the store has not advanced
    past base_version) must propagate, not be silently adopted.  Before this
    fix, a transient upload failure where the marker happened to match the
    current version was incorrectly treated as a partial-publish retry."""
    manager = _manager(world)
    _activate(manager)  # seed: all members published

    real = artifacts.publish

    def fail_with_transient_error(alias: str, base: Any, source: str) -> str:
        # Fail the config publish with a non-CAS RuntimeError while the
        # store is still at base_version (no ECONFLICT).
        if alias == activated_index_store_alias(USER, COMPOSITE):
            raise RuntimeError("upload failed: connection reset")
        return real(alias, base, source)

    monkeypatch.setattr(artifacts, "publish", fail_with_transient_error)
    with pytest.raises(RuntimeError, match="upload failed"):
        manager.publish_index(USER, COMPOSITE)


def test_non_cas_runtime_error_on_member_propagates(
    world: CompositeWorld, artifacts: FakeArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A member publish that fails with a non-CAS RuntimeError must not be
    adopted even if the member's marker matches the store's current version."""
    manager = _manager(world)
    _activate(manager)

    real = artifacts.publish
    attempt = [0]

    def fail_member_non_cas(alias: str, base: Any, source: str) -> str:
        attempt[0] += 1
        # Call 1 = config (ok), call 2 = member 1 (fail non-CAS).
        if attempt[0] == 2:
            raise RuntimeError("upload timeout")
        return real(alias, base, source)

    monkeypatch.setattr(artifacts, "publish", fail_member_non_cas)
    with pytest.raises(RuntimeError, match="upload timeout"):
        manager.publish_index(USER, COMPOSITE)


def test_delete_with_corrupt_member_versions(
    world: CompositeWorld, artifacts: FakeArtifacts
) -> None:
    """A composite whose member_index_versions is corrupt (not a Mapping)
    can still be deactivated -- delete reads discovered_repos, not
    member_index_versions."""
    manager = _manager(world)
    _activate(manager)
    meta = manager._load_metadata(USER, COMPOSITE)

    # Corrupt the member versions in the metadata.
    meta[MEMBER_INDEX_VERSIONS_FIELD] = "totally-corrupt"
    manager._save_metadata(USER, COMPOSITE, meta)

    # delete() should still succeed (it reads discovered_repos, not
    # member_index_versions).
    manager._do_deactivate_repository(USER, COMPOSITE)
    deleted = {c[1] for c in artifacts.calls if c[0] == "delete_alias"}
    assert deleted == {activated_index_store_alias(USER, COMPOSITE)} | {
        _member_alias(m) for m in MEMBERS
    }


def test_local_composite_activation_records_nothing(
    world: CompositeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        index_store_wiring, "server_index_store", lambda: LocalIndexStore()
    )
    manager = _manager(world)
    _activate(manager)
    meta = manager._load_metadata(USER, COMPOSITE)
    assert INDEX_VERSION_FIELD not in meta
    assert MEMBER_INDEX_VERSIONS_FIELD not in meta
