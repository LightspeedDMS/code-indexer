"""The refresh integrity gate's baseline on a pod that did not publish it.

Under ``index_store_backend: delegated`` the refreshing pod may never have
held the alias's current target (another pod published it). The gate's
baseline -- the published index's point counts, and the index to self-heal
from -- must then come from the IndexStore, never from "nothing":

* the pointer records the published version -> checked out to the target
  (``ensure_served_here``), counted there;
* no recorded version (a pointer from before cross-pod publishing) -> the
  store's current version, checked out to pod-local scratch, counted, used as
  the self-heal source, removed after the gate;
* nothing published: a clone-path target (first refresh after an add) has no
  previous points, as today; a snapshot target cannot be judged -> raises;
* a store / checkout failure raises.

Local store, no current target, or a pod already holding the target: today's
``_published_point_counts``. Pod paths in these tests do not exist unless
the code under test creates them.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos import refresh_scheduler as rs
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.refresh_integrity_gate import count_collection_points
from code_indexer.server.services import index_materializer as im
from code_indexer.server.startup import index_store_wiring
from code_indexer.storage.sqlite_chunk_store import ChunkStore
from code_indexer.utils.index_store import (
    INDEX_DIRNAME,
    DelegatedIndexStore,
    LocalIndexStore,
    published_index_fields,
)
from tests.unit.global_repos.test_refresh_cidx_meta_shared_content import (
    ALIAS as META_ALIAS,
    _refresh_once_with,
    _scheduler as _meta_scheduler,
    _seed_at,
    meta_world,
)
from tests.unit.global_repos.test_refresh_integrity_gate_1506 import (
    _make_chunks_db_collection,
)
from tests.unit.server.index_store_fakes import FakeArtifacts

REPO = "agent-router"
ALIAS = f"{REPO}-global"
COLLECTION = "voyage-code-3"

__all__ = ["meta_world"]  # the imported fixture, used below by name


def _records(n: int) -> List[Dict[str, Any]]:
    return [
        {"id": f"p{i}", "vector": [0.1, 0.2, 0.3, 0.4], "payload": {"path": f"{i}.py"}}
        for i in range(n)
    ]


def _built_index(root: Path, points: int) -> Path:
    """``root/.code-indexer`` with one CHUNKS_DB collection of *points*."""
    index_root = root / INDEX_DIRNAME
    _make_chunks_db_collection(index_root / "index" / COLLECTION, _records(points))
    return index_root


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    artifacts = FakeArtifacts(tmp_path / "store")
    store = DelegatedIndexStore(artifacts)
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: store)
    monkeypatch.setattr(im, "run_fix_config", lambda target: None)
    # pod A published version 1 (7 points) -- pod B holds none of it
    built = _built_index(tmp_path / "pod-a" / REPO, 7)
    version = store.publish(tmp_path / "pod-a" / REPO, built)
    artifacts.calls.clear()
    golden = tmp_path / "pod-b" / "golden-repos"
    scheduler = rs.RefreshScheduler.__new__(rs.RefreshScheduler)
    scheduler._index_store = store
    scheduler.golden_repos_dir = golden
    scheduler.alias_manager = AliasManager(str(golden / "aliases"))
    return {
        "artifacts": artifacts,
        "store": store,
        "version": version,
        "scheduler": scheduler,
        "golden": golden,
        "source": golden / REPO,
        "snapshot": golden / ".versioned" / REPO / "v_1760000000",
    }


def _point(world: Dict[str, Any], target: Path, recorded: bool) -> None:
    extra = published_index_fields(REPO, world["version"]) if recorded else None
    world["scheduler"].alias_manager.create_alias(
        ALIAS, str(target), repo_name=REPO, extra=extra
    )


def _baseline(world: Dict[str, Any], target: Any) -> Any:
    return world["scheduler"]._integrity_baseline(
        ALIAS, str(world["source"]), None if target is None else str(target)
    )


# ── delegated, the target is not on this pod ────────────────────────────────


def test_a_recorded_version_is_checked_out_to_the_target_and_counted(
    world: Dict[str, Any],
) -> None:
    _point(world, world["snapshot"], recorded=True)
    baseline = _baseline(world, world["snapshot"])
    assert baseline.points == {COLLECTION: 7}
    assert baseline.healthy_index_dir is None  # the target itself, as today
    assert count_collection_points(world["snapshot"] / INDEX_DIRNAME / "index") == {
        COLLECTION: 7
    }
    baseline.close()
    assert world["snapshot"].exists()


def test_an_unrecorded_pointer_is_judged_against_the_store_current_version(
    world: Dict[str, Any],
) -> None:
    _point(world, world["snapshot"], recorded=False)
    baseline = _baseline(world, world["snapshot"])
    scratch = world["golden"] / rs.INTEGRITY_BASELINE_DIRNAME / REPO
    assert baseline.points == {COLLECTION: 7}
    assert baseline.healthy_index_dir == scratch / INDEX_DIRNAME / "index"
    assert count_collection_points(baseline.healthy_index_dir) == {COLLECTION: 7}
    assert not world["snapshot"].exists(), "an unrecorded target was claimed"
    assert ("checkout", REPO, str(scratch / INDEX_DIRNAME), world["version"]) in world[
        "artifacts"
    ].calls
    baseline.close()
    assert not scratch.exists()


def test_a_leftover_scratch_is_replaced(world: Dict[str, Any]) -> None:
    scratch = world["golden"] / rs.INTEGRITY_BASELINE_DIRNAME / REPO
    (scratch / "stale").mkdir(parents=True)
    _point(world, world["snapshot"], recorded=False)
    baseline = _baseline(world, world["snapshot"])
    assert not (scratch / "stale").exists()
    baseline.close()


def test_nothing_published_for_a_clone_path_target_is_a_first_publish(
    world: Dict[str, Any],
) -> None:
    shutil.rmtree(world["artifacts"].root)
    world["artifacts"].versions.clear()
    _point(world, world["source"], recorded=False)
    baseline = _baseline(world, world["source"])
    assert baseline.points == {} and baseline.healthy_index_dir is None


def test_nothing_published_for_a_snapshot_target_cannot_be_judged(
    world: Dict[str, Any],
) -> None:
    world["artifacts"].versions.clear()
    _point(world, world["snapshot"], recorded=False)
    with pytest.raises(RuntimeError, match="Cannot judge"):
        _baseline(world, world["snapshot"])


@pytest.mark.parametrize("recorded", [True, False])
def test_a_store_failure_raises_and_leaves_no_scratch(
    world: Dict[str, Any], recorded: bool
) -> None:
    world["artifacts"].fail_checkout = True
    _point(world, world["snapshot"], recorded=recorded)
    with pytest.raises(Exception, match="EDIGEST"):
        _baseline(world, world["snapshot"])
    assert not (world["golden"] / rs.INTEGRITY_BASELINE_DIRNAME / REPO).exists()
    assert not world["snapshot"].exists()


def test_an_unreachable_store_raises(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(alias: str) -> None:
        raise OSError("artifact service unreachable")

    monkeypatch.setattr(world["artifacts"], "current_version", down)
    _point(world, world["snapshot"], recorded=False)
    with pytest.raises(OSError):
        _baseline(world, world["snapshot"])


def test_a_store_that_cannot_read_versions_cannot_judge(
    world: Dict[str, Any],
) -> None:
    class Bare:
        is_local = False

        def index_root(self, path: Any) -> Path:
            return Path(path) / INDEX_DIRNAME

    world["scheduler"]._index_store = Bare()
    _point(world, world["snapshot"], recorded=False)
    with pytest.raises(RuntimeError, match="cannot read a published version"):
        _baseline(world, world["snapshot"])


def test_a_pointer_to_another_target_is_not_trusted_for_this_one(
    world: Dict[str, Any],
) -> None:
    """The recorded version describes the RECORDED target only."""
    _point(world, world["golden"] / ".versioned" / REPO / "v_1", recorded=True)
    baseline = _baseline(world, world["snapshot"])
    assert baseline.healthy_index_dir is not None  # judged from the store
    assert not world["snapshot"].exists()
    baseline.close()


# ── today's baseline ────────────────────────────────────────────────────────


@pytest.mark.parametrize("recorded", [True, False])
def test_a_pod_holding_the_target_counts_it_where_it_is(
    world: Dict[str, Any], recorded: bool
) -> None:
    _built_index(world["snapshot"], 3)
    _point(world, world["snapshot"], recorded=recorded)
    baseline = _baseline(world, world["snapshot"])
    assert baseline.points == {COLLECTION: 3}
    assert world["artifacts"].calls == []


def test_no_current_target_has_no_previous_points(world: Dict[str, Any]) -> None:
    baseline = _baseline(world, None)
    assert baseline.points == {} and world["artifacts"].calls == []


def test_the_local_store_is_todays_baseline(world: Dict[str, Any]) -> None:
    scheduler = world["scheduler"]
    scheduler._index_store = LocalIndexStore()
    missing = world["golden"] / "not-on-this-pod" / "v_1"
    seen: List[Any] = []
    real = scheduler._published_point_counts
    setattr(scheduler, "_published_point_counts", lambda t: seen.append(t) or real(t))
    baseline = _baseline(world, missing)
    assert baseline.points == {} and baseline.healthy_index_dir is None
    assert seen == [str(missing)]
    assert not missing.exists() and not missing.parent.exists()
    assert world["artifacts"].calls == []


# ── the gate uses the baseline ──────────────────────────────────────────────


def test_a_collapsed_rebuild_fails_against_the_fetched_baseline(
    world: Dict[str, Any],
) -> None:
    """Pod B rebuilt an EMPTY index: against the published 7 points that is a
    collapse, so the gate refuses -- with nothing on the pod to compare
    against it would have published it."""
    _point(world, world["snapshot"], recorded=False)
    scheduler = world["scheduler"]
    scheduler._snapshot_manager = None
    scheduler._record_integrity_gate_failure = MagicMock()
    scheduler._reset_integrity_gate_quarantine = MagicMock()
    _built_index(world["source"], 0)
    baseline = _baseline(world, world["snapshot"])
    try:
        result = scheduler._run_and_publish_integrity_gate(
            alias_name=ALIAS,
            source_path=str(world["source"]),
            current_target=str(world["snapshot"]),
            previous_points=baseline.points,
            healthy_index_dir=baseline.healthy_index_dir,
        )
    finally:
        baseline.close()
    assert result.passed is False
    scheduler._record_integrity_gate_failure.assert_called_once()


def test_the_gate_self_heals_from_the_given_healthy_index(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: Dict[str, Any] = {}

    def gate(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return SimpleNamespace(passed=True, failures=[])

    monkeypatch.setattr(rs, "run_refresh_integrity_gate", gate)
    scheduler = world["scheduler"]
    scheduler._snapshot_manager = None
    scheduler._reset_integrity_gate_quarantine = MagicMock()
    scheduler._run_and_publish_integrity_gate(
        alias_name=ALIAS,
        source_path="/src",
        current_target="/target",
        previous_points={},
        healthy_index_dir=Path("/healthy"),
    )
    assert seen["healthy_index_dir"] == Path("/healthy")
    scheduler._run_and_publish_integrity_gate(
        alias_name=ALIAS, source_path="/src", current_target="/target"
    )
    assert seen["healthy_index_dir"] == Path("/target") / INDEX_DIRNAME / "index"


def test_the_refresh_gates_on_the_baseline_and_closes_it(
    meta_world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_at(meta_world, "a.md", 1_700_000_000)
    meta_world.delegated()
    scheduler = _meta_scheduler(meta_world.pod_golden)
    closed: List[bool] = []
    baseline = rs.IntegrityBaseline({COLLECTION: 3}, Path("/healthy"))
    setattr(baseline, "close", lambda: closed.append(True))
    asked: List[Any] = []
    setattr(scheduler, "_integrity_baseline", lambda *a: asked.append(a) or baseline)
    _refresh_once_with(meta_world, monkeypatch, scheduler, index=MagicMock())
    gate = scheduler._run_and_publish_integrity_gate
    assert gate.call_args.kwargs["previous_points"] == {COLLECTION: 3}
    assert gate.call_args.kwargs["healthy_index_dir"] == Path("/healthy")
    assert asked and asked[0][0] == META_ALIAS
    assert closed == [True]


def test_a_chunk_store_count_matches(tmp_path: Path) -> None:
    """Sanity: the fixture's collections are what the gate counts."""
    root = _built_index(tmp_path, 4)
    with ChunkStore(root / "index" / COLLECTION / "chunks.db", read_only=True) as s:
        assert s.count() == 4
