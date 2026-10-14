"""A published golden index is served on ANY pod, not only the publisher.

Pod A refreshes (or adds) a golden repo under ``index_store_backend:
delegated``: it publishes the built index and records the version with the
alias pointer. Pod B shares the pointer (control-plane metadata) but not A's
disk. Pinned here, against a fake artifact service holding real trees:

* pod B checks the RECORDED version out to the pointer's own path on its first
  read, runs fix-config there, and serves it; a second read is free;
* a pod that already holds the index (the publisher) does nothing;
* fail loud: no recorded version, an unreachable store, a failed checkout or
  fix-config raise -- and leave no partial tree to be trusted later;
* a crashed checkout is redone; an unexplained partial index is refused;
* superseded checkouts are scheduled for removal, the alias's previous
  target kept;
* the local store is today's behaviour: no metadata read, nothing touched,
  even for a path that does not exist on this pod.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, cast

import pytest

from code_indexer.global_repos import refresh_scheduler as rs
from code_indexer.global_repos.alias_manager import (
    AliasManager,
    resolve_alias_or_index_path,
)
from code_indexer.server.services import index_materializer as im
from code_indexer.server.startup import index_store_wiring
from code_indexer.utils.index_store import (
    INDEX_DIRNAME,
    INDEX_STORE_ALIAS_FIELD,
    INDEX_VERSION_FIELD,
    DelegatedIndexStore,
    LocalIndexStore,
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


@pytest.fixture
def world(tmp_path: Path) -> Dict[str, Any]:
    artifacts = FakeArtifacts(tmp_path / "store")
    store = DelegatedIndexStore(artifacts)
    scratch = tmp_path / "pod-a-scratch" / REPO
    v1 = store.publish(scratch, _build(scratch, b"version one"))
    shutil.rmtree(scratch)
    v2 = store.publish(scratch, _build(scratch, b"version two"))
    golden = tmp_path / "pod-b" / "golden-repos"
    target = golden / ".versioned" / REPO / "v_1760000000"
    fixes: List[Path] = []
    artifacts.calls.clear()
    return {
        "artifacts": artifacts,
        "store": store,
        "v1": v1,
        "v2": v2,
        "golden": golden,
        "target": target,
        "fixes": fixes,
        "fix": lambda p: fixes.append(Path(p)),
        "doc": {
            "target_path": str(target),
            **published_index_fields(REPO, str(v1)),
        },
    }


def _ensure(world: Dict[str, Any], **kw: Any) -> None:
    im.ensure_published_index(
        world["target"],
        kw.get("doc", world["doc"]),
        store=kw.get("store", world["store"]),
        fix_config=kw.get("fix", world["fix"]),
    )


def _hnsw(target: Path) -> bytes:
    return (target / INDEX_DIRNAME / "index" / "hnsw.bin").read_bytes()


# ── checkout on a pod that does not hold the index ──────────────────────────


def test_pod_b_checks_out_the_recorded_version_at_the_pointer_path(
    world: Dict[str, Any],
) -> None:
    assert not world["target"].exists()
    _ensure(world)
    assert _hnsw(world["target"]) == b"version one", "served the store's current"
    assert world["artifacts"].calls == [
        ("checkout", REPO, str(world["target"] / INDEX_DIRNAME), world["v1"])
    ]
    assert world["fixes"] == [world["target"]]
    assert not (world["target"] / im.IN_PROGRESS_MARKER).exists()
    assert (world["target"] / im.CHECKED_OUT_MARKER).read_text() == world["v1"]


def test_a_second_read_does_not_check_out_again(world: Dict[str, Any]) -> None:
    _ensure(world)
    _ensure(world)
    assert len(world["artifacts"].calls) == 1
    assert len(world["fixes"]) == 1


def test_the_publishing_pod_is_left_alone(world: Dict[str, Any]) -> None:
    _build(world["target"], b"built and checked out here")
    _ensure(world)
    assert world["artifacts"].calls == [] and world["fixes"] == []
    assert _hnsw(world["target"]) == b"built and checked out here"


def test_concurrent_readers_check_out_once(world: Dict[str, Any]) -> None:
    errors: List[BaseException] = []

    def read() -> None:
        try:
            _ensure(world)
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(world["artifacts"].calls) == 1


# ── fail loud ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "doc",
    [
        None,
        {"target_path": "x"},
        {INDEX_STORE_ALIAS_FIELD: REPO},
        {INDEX_VERSION_FIELD: "v1"},
        {INDEX_STORE_ALIAS_FIELD: "", INDEX_VERSION_FIELD: "v1"},
        {INDEX_STORE_ALIAS_FIELD: REPO, INDEX_VERSION_FIELD: 7},
    ],
)
def test_no_recorded_version_is_an_error_not_a_miss(
    world: Dict[str, Any], doc: Any
) -> None:
    with pytest.raises(im.PublishedIndexUnavailableError, match="no published"):
        _ensure(world, doc=doc)
    assert not world["target"].exists()
    assert world["artifacts"].calls == []


@pytest.mark.parametrize(
    "failure", ["checkout", "unknown version", "fix-config", "empty"]
)
def test_a_failed_checkout_raises_and_leaves_nothing_trusted(
    world: Dict[str, Any], failure: str
) -> None:
    fix = world["fix"]
    doc = dict(world["doc"])
    if failure == "checkout":
        world["artifacts"].fail_checkout = True
    elif failure == "unknown version":
        doc[INDEX_VERSION_FIELD] = "v" + "9" * 32
    elif failure == "fix-config":

        def fix(p: Path) -> None:
            raise subprocess.CalledProcessError(1, ["cidx"], stderr="bad")

    else:
        empty = world["artifacts"].root / REPO / world["v1"] / "index"
        shutil.rmtree(empty)

    with pytest.raises(im.PublishedIndexUnavailableError, match="Could not check out"):
        _ensure(world, doc=doc, fix=fix)
    assert not world["target"].exists(), "a partial checkout was left behind"
    # and the next reader tries again rather than trusting anything
    world["artifacts"].fail_checkout = False
    if failure == "empty":
        return
    _ensure(world)
    assert _hnsw(world["target"]) == b"version one"


def test_a_failure_in_an_existing_target_keeps_the_directory(
    world: Dict[str, Any],
) -> None:
    world["target"].mkdir(parents=True)
    (world["target"] / "keep.txt").write_text("other pod state")
    world["artifacts"].fail_checkout = True
    with pytest.raises(im.PublishedIndexUnavailableError):
        _ensure(world)
    assert (world["target"] / "keep.txt").read_text() == "other pod state"
    assert not (world["target"] / INDEX_DIRNAME).exists()


def test_a_crashed_checkout_is_redone(world: Dict[str, Any]) -> None:
    _build(world["target"], b"half written")
    (world["target"] / im.IN_PROGRESS_MARKER).write_text(world["v1"])
    _ensure(world)
    assert _hnsw(world["target"]) == b"version one"
    assert not (world["target"] / im.IN_PROGRESS_MARKER).exists()


def test_an_unexplained_partial_index_is_refused_not_replaced(
    world: Dict[str, Any],
) -> None:
    (world["target"] / INDEX_DIRNAME).mkdir(parents=True)
    (world["target"] / INDEX_DIRNAME / "config.json").write_text("mine")
    with pytest.raises(im.PublishedIndexUnavailableError, match="refusing"):
        _ensure(world)
    assert (world["target"] / INDEX_DIRNAME / "config.json").read_text() == "mine"


def test_a_store_without_alias_checkout_is_an_error(world: Dict[str, Any]) -> None:
    class Bare:
        is_local = False

        def index_root(self, p: Any) -> Path:
            return Path(p) / INDEX_DIRNAME

    with pytest.raises(im.PublishedIndexUnavailableError, match="cannot check out"):
        _ensure(world, store=Bare())


# ── pruning ─────────────────────────────────────────────────────────────────


@pytest.fixture
def scheduled(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """What pruning hands the server's CleanupManager (it never deletes)."""
    calls: List[str] = []
    monkeypatch.setattr(im, "_schedule_removal", lambda p: calls.append(str(p)))
    return calls


def test_superseded_checkouts_are_pruned_previous_and_built_ones_kept(
    world: Dict[str, Any], scheduled: List[str]
) -> None:
    ns = world["target"].parent
    old_checkout = ns / "v_1750000000"
    previous = ns / "v_1755000000"
    built_here = ns / "v_1740000000"
    for path in (old_checkout, previous):
        _build(path, b"old")
        (path / im.CHECKED_OUT_MARKER).write_text("v")
    _build(built_here, b"built by this pod's refresh")
    doc = dict(world["doc"], previous_path=str(previous))
    _ensure(world, doc=doc)
    assert scheduled == [str(old_checkout)]
    assert previous.exists(), "pruned the alias's previous target"
    assert built_here.exists(), "pruned a snapshot this pod built"


def test_a_clone_path_target_never_prunes_its_siblings(
    world: Dict[str, Any], scheduled: List[str]
) -> None:
    clone = world["golden"] / REPO
    other = world["golden"] / "v_1"
    _build(other, b"x")
    (other / im.CHECKED_OUT_MARKER).write_text("v")
    im.ensure_published_index(
        clone, world["doc"], store=world["store"], fix_config=world["fix"]
    )
    assert _hnsw(clone) == b"version one"
    assert other.exists() and scheduled == []


# ── the local store: today's behaviour ──────────────────────────────────────


def test_the_local_store_touches_nothing_even_for_a_missing_path(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "not-on-this-pod" / "v_1"

    def never(path: Path) -> None:
        raise AssertionError(f"fix-config ran for {path} under the local store")

    im.ensure_published_index(missing, None, store=LocalIndexStore(), fix_config=never)
    assert not missing.exists() and not missing.parent.exists()


# ── AliasManager.ensure_served_here / resolve_alias_or_index_path ───────────


@pytest.fixture
def aliases(world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> AliasManager:
    manager = AliasManager(str(world["golden"] / "aliases"))
    manager.create_alias(
        ALIAS,
        str(world["target"]),
        repo_name=REPO,
        extra=published_index_fields(REPO, str(world["v1"])),
    )
    monkeypatch.setattr(im, "run_fix_config", world["fix"])
    return manager


def _serve_with(monkeypatch: pytest.MonkeyPatch, store: Any) -> None:
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: store)


def test_resolving_an_alias_on_pod_b_checks_its_index_out(
    world: Dict[str, Any], aliases: AliasManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve_with(monkeypatch, world["store"])
    resolved = resolve_alias_or_index_path(aliases, ALIAS, {"index_path": "/nope"})
    assert resolved == str(world["target"])
    assert _hnsw(world["target"]) == b"version one"


def test_resolving_fails_loud_when_the_store_is_down(
    world: Dict[str, Any], aliases: AliasManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve_with(monkeypatch, world["store"])
    world["artifacts"].fail_checkout = True
    with pytest.raises(im.PublishedIndexUnavailableError):
        resolve_alias_or_index_path(aliases, ALIAS, None)


def test_local_serving_read_is_exactly_read_alias(
    world: Dict[str, Any], aliases: AliasManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve_with(monkeypatch, LocalIndexStore())
    reads: List[str] = []
    real = aliases.read_record

    def counted(name: str) -> Any:
        reads.append(name)
        return real(name)

    monkeypatch.setattr(aliases, "read_record", counted)
    aliases.ensure_served_here(ALIAS, str(world["target"]))
    assert reads == [], "the local path read the alias document"
    assert resolve_alias_or_index_path(aliases, ALIAS, None) == str(world["target"])
    assert reads == [ALIAS], "resolution read more than today's one document"
    assert not world["target"].exists()


def test_a_write_mode_redirect_is_returned_untouched(
    world: Dict[str, Any], aliases: AliasManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve_with(monkeypatch, world["store"])
    monkeypatch.setattr(
        aliases, "_read_write_mode_source_path", lambda name: "/live/source"
    )
    assert resolve_alias_or_index_path(aliases, ALIAS, None) == "/live/source"
    assert world["artifacts"].calls == []


def test_the_server_store_defaults_to_local_outside_a_server() -> None:
    assert index_store_wiring.server_index_store().is_local


# ── publishers record the version with the pointer ──────────────────────────


class _SnapshotManager:
    uses_flexclone = False

    def __init__(self, base: Path) -> None:
        self.base = base

    def get_snapshot_path(self, alias: str, timestamp: str) -> str:
        return str(self.base / ".versioned" / alias / f"v_{timestamp}")


def _scheduler(world: Dict[str, Any], store: Any) -> rs.RefreshScheduler:
    scheduler: Any = rs.RefreshScheduler.__new__(rs.RefreshScheduler)
    scheduler._index_store = store
    scheduler._snapshot_manager = _SnapshotManager(world["golden"].parent / "pod-a")
    scheduler.resource_config = None
    scheduler._pending_meta_fingerprints = {}
    scheduler._pending_index_publications = {}
    scheduler._run_subprocess = lambda *a, **k: None
    return cast(rs.RefreshScheduler, scheduler)


def test_a_refresh_swap_records_the_version_it_published(
    world: Dict[str, Any],
) -> None:
    scheduler = _scheduler(world, world["store"])
    source = world["golden"].parent / "pod-a" / REPO
    _build(source, b"refreshed")
    snapshot = scheduler._create_snapshot(ALIAS, str(source))
    extra = scheduler.published_swap_extra(ALIAS, snapshot)["extra"]
    assert extra == published_index_fields(
        REPO, str(world["artifacts"].current_version(REPO))
    )
    # pod B, from the recorded fields alone
    im.ensure_published_index(
        world["target"], extra, store=world["store"], fix_config=world["fix"]
    )
    assert _hnsw(world["target"]) == b"refreshed"


def test_a_swap_to_a_path_other_than_the_published_one_is_refused(
    world: Dict[str, Any],
) -> None:
    scheduler = _scheduler(world, world["store"])
    source = world["golden"].parent / "pod-a" / REPO
    _build(source, b"refreshed")
    scheduler._create_snapshot(ALIAS, str(source))
    with pytest.raises(RuntimeError, match="published by this refresh"):
        scheduler.published_swap_extra(ALIAS, "/somewhere/else")


def test_a_delegated_swap_with_nothing_published_is_refused(
    world: Dict[str, Any],
) -> None:
    with pytest.raises(RuntimeError, match="no index was published"):
        _scheduler(world, world["store"]).published_swap_extra(ALIAS, "/x")


def test_a_local_swap_carries_nothing_new(world: Dict[str, Any]) -> None:
    scheduler = _scheduler(world, LocalIndexStore())
    assert scheduler.published_swap_extra(ALIAS, "/x") == {}
    scheduler._pending_meta_fingerprints[ALIAS] = "fp"
    assert scheduler.published_swap_extra(ALIAS, "/x") == {
        "extra": {scheduler.META_FINGERPRINT_FIELD: "fp"}
    }


def _manager() -> Any:
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoManager,
    )

    return GoldenRepoManager.__new__(GoldenRepoManager)


def test_an_add_publishes_under_a_delegated_store(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve_with(monkeypatch, world["store"])
    clone = world["golden"].parent / "pod-a" / "golden-repos" / REPO
    _build(clone, b"added")
    fields = _manager()._publish_added_index(str(clone))
    assert fields == published_index_fields(
        REPO, str(world["artifacts"].current_version(REPO))
    )
    im.ensure_published_index(
        world["target"], fields, store=world["store"], fix_config=world["fix"]
    )
    assert _hnsw(world["target"]) == b"added"


def test_an_add_fails_when_the_publish_fails(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve_with(monkeypatch, world["store"])
    world["artifacts"].fail_publish = True
    with pytest.raises(OSError):
        _manager()._publish_added_index(str(world["golden"] / REPO))


def test_a_local_add_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve_with(monkeypatch, LocalIndexStore())
    assert _manager()._publish_added_index(str(tmp_path / "missing")) is None


def test_a_v_named_target_outside_versioned_prunes_nothing(
    world: Dict[str, Any], scheduled: List[str]
) -> None:
    target = world["golden"] / "elsewhere" / "v_2"
    sibling = target.parent / "v_1"
    _build(sibling, b"x")
    (sibling / im.CHECKED_OUT_MARKER).write_text("v")
    im.ensure_published_index(
        target, world["doc"], store=world["store"], fix_config=world["fix"]
    )
    assert sibling.exists() and scheduled == []


def test_a_publish_without_a_version_fails_the_snapshot(
    world: Dict[str, Any],
) -> None:
    class NoVersion(DelegatedIndexStore):
        def publish(self, repo_path: Any, built_dir: Path) -> Any:
            return None

    scheduler = _scheduler(world, NoVersion(world["artifacts"]))
    source = world["golden"].parent / "pod-a" / REPO
    _build(source, b"refreshed")
    with pytest.raises(RuntimeError, match="returned no version"):
        scheduler._create_snapshot(ALIAS, str(source))
    assert not any(c[0] == "checkout" for c in world["artifacts"].calls)


def test_the_server_store_is_the_one_parked_on_app_state(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.utils import registry_factory

    class State:
        index_store = world["store"]

    monkeypatch.setattr(registry_factory, "_running_server_app_state", lambda: State)
    assert index_store_wiring.server_index_store() is world["store"]
