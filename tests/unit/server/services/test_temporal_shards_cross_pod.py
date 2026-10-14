"""A golden repo's temporal shards are served on ANY pod (IndexStore, delegated).

Pod A indexes temporal data into the fixed root
``{golden_repos_dir}/.temporal/{alias}/`` and publishes it as its own
artifact (``temporal.<sha256(repo)>``), recording the version with the alias
pointer. Pod B -- whose temporal root does not exist -- serves every temporal
read from the recorded version. Pinned against a fake artifact service that
stores real trees:

* end to end: pod B's temporal search (the SemanticQueryManager read seam, a
  real FilesystemVectorStore search) returns the point pod A published;
* an older version on the pod is swapped for the recorded one (rename, with
  rollback); a store failure is an error, never "no temporal data";
* a refresh publishes the shards it rebuilt with the snapshot, in the same
  pointer write, and serves them before rebuilding; one that did not rebuild
  them leaves the recorded version alone;
* the HNSW orphan sweep repairs the recorded version and publishes the
  repair (recording via update_fields_if, serialized against swap_alias by
  a per-alias lock);
* temporal aliases never collide with golden index aliases;
* local store: today's paths, no alias read, nothing fetched.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pytest

from code_indexer.global_repos import refresh_scheduler as rs
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.services import index_materializer as im
from code_indexer.server.services import temporal_index_store as tis
from code_indexer.server.services.hnsw_orphan_sweep import scheduler as sweep
from code_indexer.server.services.hnsw_orphan_sweep.repair_executor import (
    SweepOutcome,
)
from code_indexer.server.startup import index_store_wiring
from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.utils.index_store import (
    INDEX_DIRNAME,
    TEMPORAL_INDEX_STORE_ALIAS_FIELD,
    TEMPORAL_INDEX_VERSION_FIELD,
    TEMPORAL_STORE_ALIAS_PREFIX,
    DelegatedIndexStore,
    LocalIndexStore,
    index_store_alias,
    is_temporal_store_alias,
    published_temporal_fields,
    temporal_index_store_alias,
)
from tests.unit.server.index_store_fakes import FakeArtifacts

REPO = "agent-router"
GLOBAL = f"{REPO}-global"
COLLECTION = "code-indexer-temporal-voyage_code_3-2024Q1"
VECTOR_SIZE = 8


@pytest.fixture
def artifacts(tmp_path: Path) -> FakeArtifacts:
    return FakeArtifacts(tmp_path / "store")


@pytest.fixture
def store(artifacts: FakeArtifacts, monkeypatch: pytest.MonkeyPatch) -> Any:
    delegated = DelegatedIndexStore(artifacts)
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: delegated)
    return delegated


@pytest.fixture
def golden(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "golden-repos"
    path.mkdir(parents=True)
    return path


def _root(golden: Path) -> Path:
    return server_temporal_index_root(golden, REPO)


def _build_shards(root: Path, path_payload: str) -> List[float]:
    """A REAL temporal collection with one point; returns its vector."""
    vector = np.ones(VECTOR_SIZE).tolist()
    vectors = FilesystemVectorStore(root, project_root=root)
    vectors.create_collection(COLLECTION, vector_size=VECTOR_SIZE)
    vectors.begin_indexing(COLLECTION)
    vectors.upsert_points(
        COLLECTION,
        [{"id": "c1", "vector": vector, "payload": {"path": path_payload}}],
    )
    vectors.end_indexing(COLLECTION)
    return vector


def _tree(root: Path) -> Dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _publish_from_pod_a(golden: Path, store: Any, payload: str) -> str:
    _build_shards(_root(golden), payload)
    fields = tis.publish_temporal_root(
        golden, REPO, store=store, base_version=None, use_current_base=True
    )
    AliasManager(str(golden / "aliases")).create_alias(
        GLOBAL, str(golden / REPO), repo_name=REPO, extra=fields
    )
    return fields[TEMPORAL_INDEX_VERSION_FIELD]


def _forget(golden: Path) -> None:
    """Pod B: none of the temporal state is on this pod."""
    shutil.rmtree(_root(golden))
    tis.checked_out_marker(_root(golden)).unlink(missing_ok=True)


# ── end to end: a temporal query on pod B ───────────────────────────────────


def test_pod_b_temporal_search_returns_the_published_data(
    golden: Path, store: Any
) -> None:
    from code_indexer.server.query.semantic_query_manager import (
        SemanticQueryManager,
    )

    _publish_from_pod_a(golden, store, "src/published.py")
    _forget(golden)
    assert not _root(golden).exists()

    activated = golden.parent / "activated-repos"
    activated.mkdir()
    arm = type("Arm", (), {"activated_repos_dir": str(activated)})()
    manager = SemanticQueryManager.__new__(SemanticQueryManager)
    manager.activated_repo_manager = arm
    index_dir = manager._resolve_temporal_index_dir(GLOBAL)
    assert index_dir == _root(golden)

    vectors = FilesystemVectorStore(index_dir, project_root=index_dir)
    results = vectors.search(
        query="q",
        embedding_provider=None,
        collection_name=COLLECTION,
        limit=5,
        precomputed_query_vector=np.ones(VECTOR_SIZE).tolist(),
    )
    hits = results[0] if isinstance(results, tuple) else results
    assert [hit["payload"]["path"] for hit in hits] == ["src/published.py"]


# ── serving ─────────────────────────────────────────────────────────────────


def test_pod_b_checks_out_exactly_the_recorded_version(
    golden: Path, store: Any, artifacts: FakeArtifacts
) -> None:
    version = _publish_from_pod_a(golden, store, "a.py")
    expected = _tree(_root(golden))
    _forget(golden)
    artifacts.calls.clear()
    assert tis.served_temporal_index_root(golden, GLOBAL) == _root(golden)
    assert _tree(_root(golden)) == expected
    assert tis.checked_out_marker(_root(golden)).read_text() == version
    tis.served_temporal_index_root(golden, REPO)  # second read: nothing fetched
    assert [c[0] for c in artifacts.calls] == ["checkout"]


def test_an_older_version_on_the_pod_is_swapped(golden: Path, store: Any) -> None:
    _publish_from_pod_a(golden, store, "old.py")
    old = _tree(_root(golden))
    _build_shards(golden.parent / "elsewhere", "new.py")
    newer = store.publish_as(
        temporal_index_store_alias(REPO), golden.parent / "elsewhere"
    )
    aliases = AliasManager(str(golden / "aliases"))
    assert aliases.update_fields_if(
        GLOBAL, {}, published_temporal_fields(temporal_index_store_alias(REPO), newer)
    )
    tis.served_temporal_index_root(golden, REPO)
    assert _tree(_root(golden)) != old
    assert tis.checked_out_marker(_root(golden)).read_text() == newer


def test_a_failed_swap_keeps_the_older_shards_and_raises(
    golden: Path, store: Any, artifacts: FakeArtifacts
) -> None:
    _publish_from_pod_a(golden, store, "old.py")
    old = _tree(_root(golden))
    tis.checked_out_marker(_root(golden)).write_text("an older version")
    artifacts.fail_checkout = True
    with pytest.raises(im.PublishedIndexUnavailableError, match="temporal"):
        tis.served_temporal_index_root(golden, REPO)
    assert _tree(_root(golden)) == old
    assert not (golden / ".temporal" / f".{REPO}.temporal-in-progress").exists()


def test_a_store_failure_on_pod_b_is_an_error(
    golden: Path, store: Any, artifacts: FakeArtifacts
) -> None:
    _publish_from_pod_a(golden, store, "a.py")
    _forget(golden)
    artifacts.fail_checkout = True
    with pytest.raises(im.PublishedIndexUnavailableError):
        tis.served_temporal_index_root(golden, REPO)
    assert not _root(golden).exists()


def test_a_pointer_recording_nothing_is_served_as_before(
    golden: Path, store: Any, artifacts: FakeArtifacts
) -> None:
    AliasManager(str(golden / "aliases")).create_alias(GLOBAL, str(golden / REPO))
    assert tis.served_temporal_index_root(golden, REPO) == _root(golden)
    assert artifacts.calls == []


def test_a_pointer_naming_another_alias_is_refused(golden: Path, store: Any) -> None:
    AliasManager(str(golden / "aliases")).create_alias(
        GLOBAL,
        str(golden / REPO),
        extra=published_temporal_fields(temporal_index_store_alias("other"), "v1"),
    )
    with pytest.raises(im.PublishedIndexUnavailableError, match="refusing"):
        tis.served_temporal_index_root(golden, REPO)


def test_local_store_reads_no_alias_and_fetches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        index_store_wiring, "server_index_store", lambda: LocalIndexStore()
    )
    monkeypatch.setattr(
        AliasManager, "read_record", lambda *a: pytest.fail("alias read locally")
    )
    golden = tmp_path / "nonexistent" / "golden-repos"
    assert tis.served_temporal_index_root(golden, REPO) == server_temporal_index_root(
        golden, REPO
    )
    assert not golden.exists()


# ── refresh publishes and serves ────────────────────────────────────────────


class _SnapshotManager:
    uses_flexclone = False

    def __init__(self, base: Path) -> None:
        self.base = base

    def get_snapshot_path(self, alias: str, timestamp: str) -> str:
        return str(self.base / ".versioned" / alias / f"v_{timestamp}")


def _scheduler(golden: Path, store: Any) -> Any:
    scheduler: Any = rs.RefreshScheduler.__new__(rs.RefreshScheduler)
    scheduler._index_store = store
    scheduler._snapshot_manager = _SnapshotManager(golden)
    scheduler.golden_repos_dir = golden
    scheduler.resource_config = None
    scheduler._pending_meta_fingerprints = {}
    scheduler._pending_index_publications = {}
    scheduler._run_subprocess = lambda *a, **k: None
    source = golden / REPO
    (source / INDEX_DIRNAME / "index").mkdir(parents=True, exist_ok=True)
    return scheduler


def test_a_refresh_that_rebuilt_temporal_publishes_it_with_the_snapshot(
    golden: Path, store: Any, artifacts: FakeArtifacts
) -> None:
    scheduler = _scheduler(golden, store)
    _build_shards(_root(golden), "refreshed.py")
    scheduler.note_temporal_indexed(GLOBAL)
    snapshot = scheduler._create_snapshot(GLOBAL, str(golden / REPO))
    extra = scheduler.published_swap_extra(GLOBAL, snapshot)["extra"]
    alias = temporal_index_store_alias(REPO)
    assert extra[TEMPORAL_INDEX_STORE_ALIAS_FIELD] == alias
    assert extra[TEMPORAL_INDEX_VERSION_FIELD] == artifacts.current_version(alias)
    assert "index_version" in extra


def test_a_refresh_that_did_not_rebuild_temporal_records_nothing_new(
    golden: Path, store: Any, artifacts: FakeArtifacts
) -> None:
    scheduler = _scheduler(golden, store)
    snapshot = scheduler._create_snapshot(GLOBAL, str(golden / REPO))
    extra = scheduler.published_swap_extra(GLOBAL, snapshot)["extra"]
    assert TEMPORAL_INDEX_VERSION_FIELD not in extra
    assert artifacts.current_version(temporal_index_store_alias(REPO)) is None


def test_a_failed_temporal_publish_fails_the_snapshot(
    golden: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler = _scheduler(golden, store)
    scheduler.note_temporal_indexed(GLOBAL)  # but no shards were written
    with pytest.raises(RuntimeError, match="temporal shards"):
        scheduler._create_snapshot(GLOBAL, str(golden / REPO))
    assert not (golden / ".versioned").exists() or not any(
        (golden / ".versioned").rglob("v_*")
    )


def test_a_refresh_serves_the_published_shards_before_rebuilding(
    golden: Path, store: Any
) -> None:
    _publish_from_pod_a(golden, store, "a.py")
    expected = _tree(_root(golden))
    _forget(golden)
    _scheduler(golden, store).serve_temporal_shards(GLOBAL)
    assert _tree(_root(golden)) == expected


def test_local_refresh_neither_serves_nor_notes(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler = _scheduler(golden, LocalIndexStore())
    monkeypatch.setattr(
        AliasManager, "read_record", lambda *a: pytest.fail("alias read locally")
    )
    scheduler.serve_temporal_shards(GLOBAL)
    scheduler.note_temporal_indexed(GLOBAL)
    assert getattr(scheduler, "_temporal_indexed", None) is None


# ── the HNSW orphan sweep repairs and publishes ─────────────────────────────


class _Candidate:
    def __init__(self, root: Path, kind: str = "golden_temporal") -> None:
        self.kind = kind
        self.repo_root = root
        self.alias = GLOBAL
        self.sort_key = f"{kind}:{GLOBAL}:{COLLECTION}"


def _sweeper(outcome: SweepOutcome, root: Path, order: List[str]) -> Any:
    instance = sweep.HNSWOrphanRepairSweepScheduler.__new__(
        sweep.HNSWOrphanRepairSweepScheduler
    )
    instance._activated_repo_manager = None

    def process(candidate: Any) -> SweepOutcome:
        order.append("repair")
        if outcome == SweepOutcome.REPAIRED:
            (root / COLLECTION / "repaired.marker").write_text("fixed")
        return outcome

    instance._process_fn = process
    return instance


def test_the_sweep_publishes_a_repair_of_the_recorded_version(
    golden: Path, store: Any, artifacts: FakeArtifacts
) -> None:
    base = _publish_from_pod_a(golden, store, "a.py")
    _forget(golden)  # the sweep's pod must fetch the recorded version first
    order: List[str] = []
    candidate = _Candidate(_root(golden))
    outcome = _sweeper(SweepOutcome.REPAIRED, _root(golden), order)._process_one(
        candidate
    )
    assert outcome == SweepOutcome.REPAIRED
    alias = temporal_index_store_alias(REPO)
    publishes = [c for c in artifacts.calls if c[0] == "publish"]
    assert publishes[-1][1:3] == (alias, base)
    record = AliasManager(str(golden / "aliases")).read_record(GLOBAL)
    assert record is not None
    assert record[TEMPORAL_INDEX_VERSION_FIELD] == artifacts.current_version(alias)
    assert record[TEMPORAL_INDEX_VERSION_FIELD] != base


def test_a_clean_sweep_publishes_nothing(
    golden: Path, store: Any, artifacts: FakeArtifacts
) -> None:
    _publish_from_pod_a(golden, store, "a.py")
    before = len(artifacts.calls)
    order: List[str] = []
    _sweeper(SweepOutcome.CLEAN, _root(golden), order)._process_one(
        _Candidate(_root(golden))
    )
    assert [c[0] for c in artifacts.calls[before:]] == []


def test_a_refresh_in_between_wins_over_the_repair(
    golden: Path, store: Any, artifacts: FakeArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_from_pod_a(golden, store, "a.py")
    aliases = AliasManager(str(golden / "aliases"))
    real = AliasManager.update_fields_if

    def moved(self: Any, alias: str, expected: Any, updates: Any) -> bool:
        record = self.read_record(alias)
        record[TEMPORAL_INDEX_VERSION_FIELD] = "a-refresh-published-this"
        self._metadata.put("alias", alias, record)
        return bool(real(self, alias, expected, updates))

    monkeypatch.setattr(AliasManager, "update_fields_if", moved)
    outcome = _sweeper(SweepOutcome.REPAIRED, _root(golden), [])._process_one(
        _Candidate(_root(golden))
    )
    assert outcome == SweepOutcome.REPAIRED
    record = aliases.read_record(GLOBAL)
    assert record is not None
    assert record[TEMPORAL_INDEX_VERSION_FIELD] == "a-refresh-published-this"


@pytest.mark.parametrize("failure", ["fetch", "publish"])
def test_a_sweep_store_failure_is_an_error(
    golden: Path, store: Any, artifacts: FakeArtifacts, failure: str
) -> None:
    _publish_from_pod_a(golden, store, "a.py")
    order: List[str] = []
    if failure == "fetch":
        _forget(golden)
        artifacts.fail_checkout = True
    else:
        artifacts.fail_publish = True
    outcome = _sweeper(SweepOutcome.REPAIRED, _root(golden), order)._process_one(
        _Candidate(_root(golden))
    )
    assert outcome == SweepOutcome.ERROR
    assert order == ([] if failure == "fetch" else ["repair"])


def test_unrecorded_shards_are_repaired_in_place(
    golden: Path, store: Any, artifacts: FakeArtifacts
) -> None:
    AliasManager(str(golden / "aliases")).create_alias(GLOBAL, str(golden / REPO))
    _build_shards(_root(golden), "a.py")
    order: List[str] = []
    assert (
        _sweeper(SweepOutcome.REPAIRED, _root(golden), order)._process_one(
            _Candidate(_root(golden))
        )
        == SweepOutcome.REPAIRED
    )
    assert order == ["repair"] and artifacts.calls == []


@pytest.mark.parametrize("kind", ["golden", "activated"])
def test_other_candidates_are_untouched(
    golden: Path, store: Any, artifacts: FakeArtifacts, kind: str
) -> None:
    _publish_from_pod_a(golden, store, "a.py")
    artifacts.calls.clear()
    order: List[str] = []
    _sweeper(SweepOutcome.REPAIRED, _root(golden), order)._process_one(
        _Candidate(_root(golden), kind=kind)
    )
    assert order == ["repair"] and artifacts.calls == []


def test_local_sweep_repairs_temporal_in_place(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        index_store_wiring, "server_index_store", lambda: LocalIndexStore()
    )
    monkeypatch.setattr(
        AliasManager, "read_record", lambda *a: pytest.fail("alias read locally")
    )
    _build_shards(_root(golden), "a.py")
    order: List[str] = []
    _sweeper(SweepOutcome.REPAIRED, _root(golden), order)._process_one(
        _Candidate(_root(golden))
    )
    assert order == ["repair"]


# ── namespace ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "golden_name", ["temporal.x", TEMPORAL_STORE_ALIAS_PREFIX + "0" * 40, "Temporal.Y"]
)
def test_golden_aliases_never_enter_the_temporal_namespace(golden_name: str) -> None:
    alias = index_store_alias(golden_name)
    assert not alias.startswith(TEMPORAL_STORE_ALIAS_PREFIX)
    assert not is_temporal_store_alias(alias)


def test_temporal_aliases_are_per_repo_and_store_valid() -> None:
    names = {temporal_index_store_alias(r) for r in ("a", "b", "A", "a-global")}
    assert len(names) == 4
    assert all(is_temporal_store_alias(n) and len(n) <= 128 for n in names)
    assert not any(n.startswith("activated.") for n in names)
    with pytest.raises(ValueError):
        temporal_index_store_alias("")


def test_ensure_does_nothing_under_the_local_store(
    golden: Path, store: Any, artifacts: FakeArtifacts
) -> None:
    _publish_from_pod_a(golden, store, "a.py")
    _forget(golden)
    document = AliasManager(str(golden / "aliases")).read_record(GLOBAL)
    tis.ensure_temporal_root(_root(golden), document, REPO, store=LocalIndexStore())
    assert not _root(golden).exists()


def test_the_recorded_version_is_served_not_the_newest(
    golden: Path, store: Any
) -> None:
    _publish_from_pod_a(golden, store, "recorded.py")
    recorded = _tree(_root(golden))
    _build_shards(golden.parent / "unrecorded", "newer.py")
    store.publish_as(temporal_index_store_alias(REPO), golden.parent / "unrecorded")
    _forget(golden)
    tis.served_temporal_index_root(golden, REPO)
    assert _tree(_root(golden)) == recorded


def test_an_interrupted_swap_is_redone(
    golden: Path, store: Any, artifacts: FakeArtifacts
) -> None:
    _publish_from_pod_a(golden, store, "a.py")
    (golden / ".temporal" / f".{REPO}.temporal-in-progress").write_text("x")
    artifacts.calls.clear()
    tis.served_temporal_index_root(golden, REPO)
    assert [c[0] for c in artifacts.calls] == ["checkout"]
    assert not (golden / ".temporal" / f".{REPO}.temporal-in-progress").exists()


def test_a_failure_after_the_swap_restores_the_older_shards(
    golden: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_from_pod_a(golden, store, "old.py")
    old = _tree(_root(golden))
    tis.checked_out_marker(_root(golden)).write_text("an older version")
    real_rename = tis.os.rename
    root_renames = 0

    def rename(src: Any, dst: Any) -> None:
        nonlocal root_renames
        root_str = str(_root(golden))
        if str(dst) == root_str:
            root_renames += 1
            # The first rename targeting root is the symlink attempt that
            # gets IsADirectoryError (root is a real dir on the first swap).
            # The second is the fallback link step — fail it.
            if root_renames == 2:
                raise OSError("EIO")
        real_rename(src, dst)

    monkeypatch.setattr(tis.os, "rename", rename)
    with pytest.raises(im.PublishedIndexUnavailableError, match="EIO"):
        tis.served_temporal_index_root(golden, REPO)
    monkeypatch.setattr(tis.os, "rename", real_rename)
    assert _tree(_root(golden)) == old
    assert not (golden / ".temporal" / f".{REPO}.temporal-in-progress").exists()


# ── every other temporal reader serves first ────────────────────────────────


def test_pod_b_temporal_status_sees_the_published_shards(
    golden: Path, store: Any
) -> None:
    from code_indexer.services.temporal.temporal_status import (
        get_temporal_repo_status,
    )

    _publish_from_pod_a(golden, store, "a.py")
    _forget(golden)
    status = get_temporal_repo_status(golden, REPO, golden / REPO / INDEX_DIRNAME)
    assert status.has_data and status.resolved_path is not None
    assert Path(status.resolved_path).parent == _root(golden)


def test_pod_b_health_discovers_the_published_collections(
    golden: Path, store: Any
) -> None:
    from code_indexer.server.services.repository_health_aggregator import (
        discover_sister_temporal_collections,
    )

    _publish_from_pod_a(golden, store, "a.py")
    _forget(golden)
    found = discover_sister_temporal_collections(
        golden, REPO, golden / REPO / INDEX_DIRNAME
    )
    assert [Path(entry[2]).parent for entry in found] == [_root(golden) / COLLECTION]


def test_the_live_temporal_worker_serves_the_golden_shards(
    golden: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.services import temporal_worker

    _publish_from_pod_a(golden, store, "a.py")
    expected = _tree(_root(golden))
    _forget(golden)
    activated = golden.parent / "activated-repos"
    activated.mkdir()
    manager = type("Arm", (), {"activated_repos_dir": str(activated)})()
    monkeypatch.setattr(
        temporal_worker, "_resolve_lineage_repo_manager", lambda m: manager
    )
    worker_input = type("In", (), {"username": "alice", "repository_alias": GLOBAL})()
    context = temporal_worker._resolve_golden_temporal_context(worker_input, "job-1")
    assert context.temporal_index_dir is not None
    assert Path(context.temporal_index_dir) == _root(golden)
    assert _tree(_root(golden)) == expected


# ── the real call sites, driven end to end ──────────────────────────────────


def _temporal_events(artifacts: FakeArtifacts) -> List[str]:
    """``checkout`` / ``temporal`` / ``publish`` in the order they happened,
    for this repo's temporal alias only (the golden index has its own)."""
    alias = temporal_index_store_alias(REPO)
    return [
        call[0]
        for call in artifacts.calls
        if call[0] == "temporal" or (len(call) > 1 and call[1] == alias)
    ]


class _ChildFailed(Exception):
    pass


def _fake_index_children(golden: Path, artifacts: FakeArtifacts, fail: bool) -> Any:
    """``run_with_popen_progress`` for every cidx child: the temporal one
    records itself, requires the published shards to be on the pod already
    (pod B had none) and adds to them, as an incremental pass would -- or,
    with ``fail``, dies part-way through."""

    def run(*, command: Any, phase_name: str, **kwargs: Any) -> int:
        if phase_name == "temporal":
            artifacts.calls.append(("temporal",))
            if not (_root(golden) / COLLECTION).is_dir():
                raise _ChildFailed("temporal child ran without the served shards")
            (_root(golden) / COLLECTION / "incremental.marker").write_text("new")
            if fail:
                raise _ChildFailed("temporal child died part-way")
        return 100

    return run


def _published_tree(artifacts: FakeArtifacts) -> Dict[str, bytes]:
    alias = temporal_index_store_alias(REPO)
    version = artifacts.current_version(alias)
    assert version is not None
    return _tree(artifacts.root / alias / version)


def _pod_b(golden: Path, store: Any, artifacts: FakeArtifacts, legacy: bool) -> None:
    """Pod A published; pod B holds none of it. ``legacy``: the repo also has
    in-repo temporal state, which short-circuits the readers (max_commits
    fallback, the --clear decision) that would otherwise serve incidentally,
    so the call site's own serve is the only one."""
    _publish_from_pod_a(golden, store, "a.py")
    _forget(golden)
    if legacy:
        legacy_dir = golden / REPO / INDEX_DIRNAME / "index" / "code-indexer-temporal"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "temporal_meta.json").write_text('{"max_commits": 5}')
        vectors = FilesystemVectorStore(legacy_dir.parent, project_root=golden / REPO)
        vectors.create_collection("code-indexer-temporal", vector_size=VECTOR_SIZE)
        vectors.begin_indexing("code-indexer-temporal")
        vectors.upsert_points(
            "code-indexer-temporal",
            [{"id": "l1", "vector": np.ones(VECTOR_SIZE).tolist(), "payload": {}}],
        )
        vectors.end_indexing("code-indexer-temporal")
    artifacts.calls.clear()


def _refresh(golden: Path, store: Any, artifacts: FakeArtifacts, fail: bool) -> Any:
    """The real ``RefreshScheduler._index_source`` then ``_create_snapshot``
    (the publish), with only the cidx children faked."""
    from unittest.mock import MagicMock, patch

    from code_indexer.config import ConfigManager
    from code_indexer.global_repos.cleanup_manager import CleanupManager
    from code_indexer.global_repos.query_tracker import QueryTracker
    from code_indexer.server.utils.config_manager import ServerConfig

    registry = MagicMock()
    registry.get_global_repo.return_value = {
        "alias": GLOBAL,
        "repo_url": "git@github.com:org/repo.git",
        "enable_temporal": True,
        "temporal_options": None,
        "enable_scip": False,
    }
    tracker = QueryTracker()
    scheduler = rs.RefreshScheduler(
        golden_repos_dir=str(golden),
        config_source=ConfigManager(golden.parent / ".code-indexer" / "config.json"),
        query_tracker=tracker,
        cleanup_manager=CleanupManager(tracker),
        registry=registry,
    )
    stub = _scheduler(golden, store)
    for name in (
        "_index_store",
        "_snapshot_manager",
        "_pending_meta_fingerprints",
        "_pending_index_publications",
        "_run_subprocess",
    ):
        setattr(scheduler, name, getattr(stub, name))
    source = golden / REPO
    (source / ".git").mkdir(exist_ok=True)
    server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")
    with (
        patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=_fake_index_children(golden, artifacts, fail),
        ),
        patch("code_indexer.global_repos.refresh_scheduler.get_config_service") as a,
        patch("code_indexer.server.services.config_service.get_config_service") as b,
    ):
        a.return_value.get_config.return_value = server_config
        b.return_value.get_config.return_value = server_config
        try:
            scheduler._index_source(alias_name=GLOBAL, source_path=str(source))
        except Exception:
            if not fail:
                raise
        # The next job's snapshot (or this one's, on success) publishes.
        snapshot = scheduler._create_snapshot(GLOBAL, str(source))
    return scheduler.published_swap_extra(GLOBAL, snapshot)["extra"]


def _add_index(golden: Path, store: Any, artifacts: FakeArtifacts, fail: bool) -> Any:
    """The real add-index background worker (temporal); the refresh scheduler
    it drives is a spy whose serve / note / snapshot are the real methods.
    Returns the pointer fields the job's snapshot recorded."""
    from unittest.mock import MagicMock, Mock, patch

    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoManager,
    )
    from code_indexer.server.utils.config_manager import ServerConfig

    real = _scheduler(golden, store)
    scheduler = MagicMock()
    scheduler.acquire_write_lock.return_value = True
    scheduler.alias_manager.read_alias.return_value = str(golden / REPO)
    scheduler.serve_temporal_shards.side_effect = real.serve_temporal_shards
    scheduler.note_temporal_indexed.side_effect = real.note_temporal_indexed
    scheduler._create_snapshot.side_effect = real._create_snapshot
    scheduler.published_swap_extra.side_effect = real.published_swap_extra

    manager: Any = GoldenRepoManager(data_dir=str(golden.parent))
    repo_path = golden / REPO
    golden_repo = Mock(alias=REPO, clone_path=str(repo_path), temporal_options={})
    golden_repo.enable_temporal = False
    manager.golden_repos = {REPO: golden_repo}
    manager.get_actual_repo_path = Mock(return_value=str(repo_path))
    manager._sqlite_backend = Mock()
    manager._sqlite_backend.update_enable_temporal = Mock(return_value=True)
    manager._sqlite_backend.get_repo = Mock(
        return_value={
            "alias": REPO,
            "repo_url": "https://example.com/agent-router.git",
            "default_branch": "main",
            "clone_path": str(repo_path),
            "created_at": "2026-01-01T00:00:00+00:00",
            "enable_temporal": False,
            "temporal_options": {},
            "category_id": None,
            "category_auto_assigned": False,
        }
    )
    manager._global_repos_backend = Mock()
    workers: List[Any] = []
    manager.background_job_manager = Mock()

    def submit_job(
        operation_type: str, func: Any, submitter_username: str, **kw: Any
    ) -> str:
        workers.append(func)
        return "job"

    manager.background_job_manager.submit_job.side_effect = submit_job
    manager._refresh_scheduler = scheduler
    server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")
    with (
        patch(
            "code_indexer.server.repositories.golden_repo_manager.subprocess.run",
            return_value=Mock(returncode=0, stdout="", stderr=""),
        ),
        patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=_fake_index_children(golden, artifacts, fail),
        ),
        patch("code_indexer.server.services.config_service.get_config_service") as c,
    ):
        c.return_value.get_config.return_value = server_config
        manager.add_indexes_to_golden_repo(
            alias=REPO, index_types=["temporal"], submitter_username="admin"
        )
        try:
            workers[0]()
        except Exception:
            if not fail:
                raise
            # The next job's snapshot, on the same scheduler.
            snapshot = real._create_snapshot(GLOBAL, str(repo_path))
            return real.published_swap_extra(GLOBAL, snapshot)["extra"]
    return scheduler.alias_manager.swap_alias.call_args.kwargs["extra"]


_CALL_SITES = {"refresh": _refresh, "add-index": _add_index}


@pytest.mark.parametrize("legacy", [False, True], ids=["plain", "legacy-state"])
@pytest.mark.parametrize("site", sorted(_CALL_SITES))
def test_a_temporal_job_serves_indexes_then_publishes(
    golden: Path, store: Any, artifacts: FakeArtifacts, site: str, legacy: bool
) -> None:
    _pod_b(golden, store, artifacts, legacy)
    extra = _CALL_SITES[site](golden, store, artifacts, fail=False)
    assert _temporal_events(artifacts) == ["checkout", "temporal", "publish"]
    alias = temporal_index_store_alias(REPO)
    assert extra[TEMPORAL_INDEX_VERSION_FIELD] == artifacts.current_version(alias)
    assert f"{COLLECTION}/incremental.marker" in _published_tree(artifacts)


@pytest.mark.parametrize("site", sorted(_CALL_SITES))
def test_a_failed_temporal_child_is_never_published(
    golden: Path, store: Any, artifacts: FakeArtifacts, site: str
) -> None:
    """A child that dies part-way leaves partial shards on the pod; neither
    this job nor the next one on the same scheduler may publish them."""
    _pod_b(golden, store, artifacts, legacy=False)
    extra = _CALL_SITES[site](golden, store, artifacts, fail=True)
    assert _temporal_events(artifacts) == ["checkout", "temporal"]
    assert TEMPORAL_INDEX_VERSION_FIELD not in extra


def test_pod_b_multi_repo_temporal_search_returns_the_published_data(
    golden: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real MultiSearchService temporal path; only the embedder-bound
    fusion step and config loading are faked, at their boundary. The fake
    fusion searches the vector store the service built over ``index_path``,
    so it finds the point only if the published shards were served there."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    from code_indexer.config import ConfigManager
    from code_indexer.server.multi import multi_search_service as mss
    from code_indexer.server.utils import server_managed_provider_settings
    from code_indexer.services.temporal import temporal_fusion_dispatch

    _publish_from_pod_a(golden, store, "src/published.py")
    _forget(golden)
    seen: Dict[str, Any] = {}

    def fusion(*, index_path: Path, vector_store: Any, **kwargs: Any) -> Any:
        seen["index_path"] = Path(index_path)
        if not (Path(index_path) / COLLECTION).is_dir():
            return SimpleNamespace(results=[], warning="no temporal index")
        found = vector_store.search(
            query="q",
            embedding_provider=None,
            collection_name=COLLECTION,
            limit=5,
            precomputed_query_vector=np.ones(VECTOR_SIZE).tolist(),
        )
        hits = found[0] if isinstance(found, tuple) else found
        return SimpleNamespace(
            warning=None,
            results=[
                SimpleNamespace(
                    file_path=hit["payload"]["path"],
                    score=hit["score"],
                    content="",
                    metadata={"commit_hash": "c1"},
                    temporal_context={},
                )
                for hit in hits
            ],
        )

    monkeypatch.setattr(
        temporal_fusion_dispatch, "execute_temporal_query_with_fusion", fusion
    )
    monkeypatch.setattr(
        ConfigManager, "load_verified_config", staticmethod(lambda path: Mock())
    )
    monkeypatch.setattr(
        server_managed_provider_settings,
        "enforce_server_managed_provider_settings",
        lambda config: None,
    )
    service: Any = mss.MultiSearchService.__new__(mss.MultiSearchService)
    service.config = Mock(max_results_per_repo=10)
    service.hnsw_index_cache = None
    service._get_repository_path = lambda repo_id: str(golden / REPO)
    request = Mock(
        query="q",
        limit=10,
        language=None,
        path_filter=None,
        no_embedding_cache_shortcut=False,
        temporal_embedder=None,
    )

    results = service._search_temporal_sync(GLOBAL, request)

    assert seen["index_path"] == _root(golden)
    assert [r["file_path"] for r in results] == ["src/published.py"]


# ── pointer writes keep the temporal record (shards live outside snapshots) ──


def test_an_index_swap_keeps_the_recorded_temporal_shards(tmp_path: Path) -> None:
    from code_indexer.utils.index_store import published_temporal_fields

    aliases = AliasManager(str(tmp_path / "aliases"))
    temporal = published_temporal_fields(temporal_index_store_alias("r"), "t1")
    aliases.create_alias("r-global", "/v1", extra=temporal)
    aliases.swap_alias("r-global", "/v2", "/v1")
    record = aliases.read_record("r-global")
    assert record is not None
    assert {k: record[k] for k in temporal} == temporal


def test_a_branch_change_swap_carries_the_temporal_record_over(
    tmp_path: Path,
) -> None:
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoManager,
    )
    from code_indexer.utils.index_store import (
        published_index_fields,
        published_temporal_fields,
    )

    aliases = AliasManager(str(tmp_path / "aliases"))
    temporal = published_temporal_fields(temporal_index_store_alias("r"), "t1")
    aliases.create_alias(
        "r-global", str(tmp_path / "r"), extra={**temporal, "index_version": "v1"}
    )
    manager: Any = GoldenRepoManager.__new__(GoldenRepoManager)
    manager.golden_repos_dir = str(tmp_path)
    manager._snapshot_manager = None
    index = published_index_fields("r", "v2")
    manager._cb_swap_alias("r", str(tmp_path / ".versioned" / "r" / "v_2"), index)
    record = aliases.read_record("r-global")
    assert record is not None
    assert {k: record[k] for k in temporal} == temporal
    assert {k: record[k] for k in index} == index


# ── atomic symlink swap and versioned-directory cleanup ─────────────────────


def test_swap_uses_symlink_to_versioned_dir(golden: Path, store: Any) -> None:
    """After a swap, ``root`` is a symlink to a versioned directory so that
    subsequent swaps are a single atomic ``os.rename`` of one symlink over
    another, with no window where ``root`` is absent."""
    _publish_from_pod_a(golden, store, "v1.py")
    _build_shards(golden.parent / "elsewhere", "v2.py")
    alias = temporal_index_store_alias(REPO)
    version2 = store.publish_as(alias, golden.parent / "elsewhere")
    aliases = AliasManager(str(golden / "aliases"))
    aliases.update_fields_if(GLOBAL, {}, published_temporal_fields(alias, version2))
    tis.checked_out_marker(_root(golden)).write_text("stale")
    tis.served_temporal_index_root(golden, REPO)
    root = _root(golden)
    assert root.is_symlink(), "root must be a symlink after swap"
    assert root.resolve().name.startswith(f".{REPO}{tis._VERSIONED_PREFIX}")
    assert tis.checked_out_marker(root).read_text() == version2


def test_next_swap_cleans_previous_versioned_dir(golden: Path, store: Any) -> None:
    """Old versioned directories from previous swaps are cleaned up."""
    alias = temporal_index_store_alias(REPO)
    _publish_from_pod_a(golden, store, "v1.py")
    # First swap: v1 → v2
    _build_shards(golden.parent / "v2", "v2.py")
    v2 = store.publish_as(alias, golden.parent / "v2")
    aliases = AliasManager(str(golden / "aliases"))
    aliases.update_fields_if(GLOBAL, {}, published_temporal_fields(alias, v2))
    tis.checked_out_marker(_root(golden)).write_text("stale")
    tis.served_temporal_index_root(golden, REPO)
    root = _root(golden)
    v2_dir = root.resolve()
    assert v2_dir.is_dir()
    # Write a sentinel so we can prove this specific directory was cleaned
    sentinel = v2_dir / ".sentinel"
    sentinel.write_text("v2-was-here")

    # Second swap: v2 → v3; the v2 versioned dir must be cleaned
    _build_shards(golden.parent / "v3", "v3.py")
    v3 = store.publish_as(alias, golden.parent / "v3")
    aliases.update_fields_if(GLOBAL, {}, published_temporal_fields(alias, v3))
    tis.checked_out_marker(root).write_text("stale-again")
    tis.served_temporal_index_root(golden, REPO)
    assert not v2_dir.exists(), "previous versioned dir must be cleaned"
    assert not sentinel.exists(), "sentinel proves the old dir was removed"
    assert tis.checked_out_marker(root).read_text() == v3
    assert root.is_symlink()
    assert root.resolve() != v2_dir


def test_swapping_one_alias_does_not_remove_another_alias_versioned_dir(
    golden: Path, store: Any, artifacts: FakeArtifacts,
) -> None:
    """Regression: versioned directories are scoped by alias, so swapping
    repo A's temporal shards never removes repo B's live target."""
    REPO_A, REPO_B = "repo-a", "repo-b"
    aliases = AliasManager(str(golden / "aliases"))

    def _setup(repo: str) -> Tuple[Path, str]:
        root = server_temporal_index_root(golden, repo)
        _build_shards(root, f"{repo}.py")
        fields = tis.publish_temporal_root(
            golden, repo, store=store, base_version=None, use_current_base=True
        )
        aliases.create_alias(
            f"{repo}-global", str(golden / repo), repo_name=repo, extra=fields
        )
        return root, fields[TEMPORAL_INDEX_VERSION_FIELD]

    root_a, v_a = _setup(REPO_A)
    root_b, v_b = _setup(REPO_B)

    _build_shards(golden.parent / "a-new", "a-new.py")
    alias_a = temporal_index_store_alias(REPO_A)
    v_a2 = store.publish_as(alias_a, golden.parent / "a-new")
    aliases.update_fields_if(
        f"{REPO_A}-global", {}, published_temporal_fields(alias_a, v_a2)
    )
    tis.checked_out_marker(root_a).write_text("stale")
    tis.served_temporal_index_root(golden, REPO_A)

    assert root_a.is_symlink()
    assert tis.checked_out_marker(root_a).read_text() == v_a2
    assert root_b.is_dir(), "repo B's root must still exist after repo A swaps"
    assert tis.checked_out_marker(root_b).read_text() == v_b


# ── update_fields_if non-concurrent correctness ───────────────────────────


def test_update_fields_if_applies_when_fields_match(tmp_path: Path) -> None:
    """Non-concurrent case: update succeeds when expected fields match."""
    aliases = AliasManager(str(tmp_path / "aliases"))
    aliases.create_alias("r-global", "/v1", extra={"color": "blue"})
    applied = aliases.update_fields_if(
        "r-global",
        {"color": "blue"},
        {"color": "red", "size": "large"},
    )
    assert applied is True
    record = aliases.read_record("r-global")
    assert record is not None
    assert record["color"] == "red"
    assert record["size"] == "large"


def test_update_fields_if_rejects_when_fields_differ(tmp_path: Path) -> None:
    """Non-concurrent case: update is refused when expected fields diverge."""
    aliases = AliasManager(str(tmp_path / "aliases"))
    aliases.create_alias("r-global", "/v1", extra={"color": "blue"})
    applied = aliases.update_fields_if(
        "r-global",
        {"color": "green"},
        {"color": "red"},
    )
    assert applied is False
    record = aliases.read_record("r-global")
    assert record is not None
    assert record["color"] == "blue"


def test_swap_alias_during_update_fields_if_is_not_lost(tmp_path: Path) -> None:
    """A swap_alias landing while update_fields_if holds the per-alias lock
    must block until the update completes, so the swap's newer document is
    never overwritten by the update's stale copy."""
    import threading

    aliases = AliasManager(str(tmp_path / "aliases"))
    aliases.create_alias("r-global", "/v1", extra={"color": "blue"})

    # Inject a delay into AliasManager.read_record that fires only on the
    # FIRST call made under update_fields_if.  While that delay holds, a
    # second thread attempts swap_alias; the per-alias lock must prevent
    # it from writing between the read and the put.
    original_read = AliasManager.read_record
    delay_armed = threading.Event()
    delay_armed.set()  # armed initially
    swap_started = threading.Event()
    swap_done = threading.Event()

    def slow_read(self: Any, alias_name: str) -> Any:
        result = original_read(self, alias_name)
        if delay_armed.is_set():
            delay_armed.clear()  # fire once
            swap_started.set()   # tell the swap thread to go
            swap_done.wait(timeout=5)  # hold the lock open
        return result

    swap_error: List[BaseException] = []

    def do_swap() -> None:
        try:
            swap_started.wait(timeout=5)
            # The per-alias lock is held by update_fields_if's slow_read,
            # so this must block until the update releases the lock.
            aliases.swap_alias("r-global", "/v2", "/v1")
        except Exception as exc:
            swap_error.append(exc)
        finally:
            swap_done.set()

    t = threading.Thread(target=do_swap)
    t.start()

    AliasManager.read_record = slow_read  # type: ignore[assignment]
    applied = aliases.update_fields_if(
        "r-global", {"color": "blue"}, {"color": "red"}
    )
    AliasManager.read_record = original_read  # type: ignore[assignment]
    t.join(timeout=10)

    assert applied is True, "update_fields_if should have succeeded"
    assert not swap_error, f"swap_alias raised: {swap_error}"
    record = aliases.read_record("r-global")
    assert record is not None
    # swap_alias ran AFTER update_fields_if finished, so the final document
    # must reflect the swap (target_path="/v2"), not the update's write.
    assert record["target_path"] == "/v2", (
        "swap_alias's write must survive -- the per-alias lock must have "
        "prevented it from interleaving with update_fields_if"
    )


@pytest.mark.parametrize(
    "document",
    [
        None,
        {},
        {"temporal_index_store_alias": "temporal.x"},
        {"temporal_index_version": ""},
    ],
)
def test_an_incomplete_temporal_record_is_not_carried(document: Any) -> None:
    from code_indexer.utils.index_store import recorded_temporal_fields

    assert recorded_temporal_fields(document) == {}
