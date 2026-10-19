"""Caches of the projection engine: model, projected documents, universe.

Real directories (a live one and one under a .versioned snapshot layout)
and a real SQLite registry. Neutral names only.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from code_indexer.server.services.cidx_meta_access import (
    AliasUniverse,
    MetaVisibility,
    load_depmap_model,
    project_lines,
    projection_cache_stats,
    resolve_meta_visibility,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    CANARY,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
    read_lines,
)


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path)


def _retarget_ledger(dep_map_dir: Path) -> None:
    ledger = dep_map_dir / "ledger.md"
    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace(
            f"  - {ALPHA}\n", f"  - {CANARY}\n", 1
        ),
        encoding="utf-8",
    )


def test_unchanged_live_directory_reuses_the_model(fx: CidxMetaFixture) -> None:
    assert load_depmap_model(fx.dep_map_dir) is load_depmap_model(fx.dep_map_dir)


def test_live_directory_edit_yields_a_fresh_model(fx: CidxMetaFixture) -> None:
    before = load_depmap_model(fx.dep_map_dir)
    _retarget_ledger(fx.dep_map_dir)
    after = load_depmap_model(fx.dep_map_dir)
    assert after is not before
    assert before.participants["ledger"] == frozenset({ALPHA})
    assert after.participants["ledger"] == frozenset({CANARY})


def test_immutable_snapshot_is_parsed_once_per_process(
    fx: CidxMetaFixture, tmp_path: Path
) -> None:
    snapshot = tmp_path / "golden" / ".versioned" / "cidx-meta" / "v_1700000000"
    snapshot_dep_map = snapshot / "dependency-map"
    shutil.copytree(fx.dep_map_dir, snapshot_dep_map)

    first = load_depmap_model(snapshot_dep_map)
    _retarget_ledger(snapshot_dep_map)
    second = load_depmap_model(snapshot_dep_map)

    assert second is first
    assert second.participants["ledger"] == frozenset({ALPHA})


def test_equal_visible_sets_share_projections_and_different_never(
    fx: CidxMetaFixture,
) -> None:
    lines = read_lines(fx.root / f"{ALPHA}.md") + [f"Unique to {fx.root}."]
    model = load_depmap_model(fx.dep_map_dir)
    universe = AliasUniverse(fx.registry).snapshot()
    user_vis = resolve_meta_visibility(RESTRICTED_USER, fx.access_service)
    same_set = MetaVisibility.restricted([f"{ALPHA}-global"])
    other_set = MetaVisibility.restricted([CANARY])

    start = projection_cache_stats()
    first = project_lines(lines, f"{ALPHA}.md", user_vis, model, universe)
    shared = project_lines(lines, f"{ALPHA}.md", same_set, model, universe)
    after_shared = projection_cache_stats()
    other = project_lines(lines, f"{ALPHA}.md", other_set, model, universe)
    end = projection_cache_stats()

    assert shared == first
    assert after_shared.projections_computed == start.projections_computed + 1
    assert after_shared.projection_hits == start.projection_hits + 1
    assert end.projections_computed == after_shared.projections_computed + 1
    assert other != first


class _ListThenUnlink:
    """``os.scandir`` that lists the real directory, then removes *victim*
    from disk before the caller stats the listed entries (once)."""

    def __init__(self, victim: Path) -> None:
        self._victim = victim
        self._real = os.scandir
        self.calls = 0

    def __call__(self, path: Any) -> Any:
        self.calls += 1
        listing = list(self._real(path))
        if self._victim.exists():
            self._victim.unlink()
        return _Listing(listing)


class _Listing:
    def __init__(self, entries: List[Any]) -> None:
        self._entries = entries

    def __enter__(self) -> List[Any]:
        return self._entries

    def __exit__(self, *_exc: Any) -> None:
        return None


def test_document_vanishing_during_fingerprint_is_recomputed(
    fx: CidxMetaFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    scandir = _ListThenUnlink(fx.dep_map_dir / "ledger.md")
    monkeypatch.setattr(os, "scandir", scandir)
    model = load_depmap_model(fx.dep_map_dir)
    assert scandir.calls >= 2
    monkeypatch.undo()
    assert load_depmap_model(fx.dep_map_dir) is model


def test_fingerprint_fails_loudly_when_the_directory_never_settles(
    fx: CidxMetaFixture,
) -> None:
    (fx.dep_map_dir / "ghost.md").symlink_to(fx.dep_map_dir / "missing-target.md")
    with pytest.raises(RuntimeError, match="kept changing"):
        load_depmap_model(fx.dep_map_dir)


class _CountingRegistry:
    """Delegates to the real registry and counts its reads."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self.reads = 0

    def list_repos(self) -> Dict[str, Dict[str, Any]]:
        self.reads += 1
        result: Dict[str, Dict[str, Any]] = self._registry.list_repos()
        return result


def test_alias_universe_is_read_once_per_ttl(fx: CidxMetaFixture) -> None:
    now: List[float] = [1000.0]
    registry = _CountingRegistry(fx.registry)
    universe = AliasUniverse(registry, ttl_seconds=60, clock=lambda: now[0])

    first = universe.snapshot()
    now[0] += 59
    assert universe.snapshot() is first
    assert registry.reads == 1
    assert CANARY in first.names and "cidx-meta" in first.names

    now[0] += 2
    unchanged = universe.snapshot()
    assert registry.reads == 2
    assert unchanged.generation == first.generation

    fx.registry.register_repo(
        alias_name="delta-sample-global",
        repo_name="delta-sample",
        repo_url=None,
        index_path="/golden/delta-sample",
    )
    now[0] += 61
    changed = universe.snapshot()
    assert changed.generation == first.generation + 1
    assert "delta-sample" in changed.names
    assert changed.digest != first.digest


class _GatedRegistry:
    """Delegates to the real registry; reads after the first ``free`` ones
    block until ``gate`` opens (a stalled PostgreSQL), or raise when
    ``failure`` is set."""

    def __init__(self, registry: Any, free: int) -> None:
        self._registry = registry
        self._free = free
        self.reads = 0
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.failure: Optional[Exception] = None

    def list_repos(self) -> Dict[str, Dict[str, Any]]:
        self.reads += 1
        if self.reads > self._free:
            self.entered.set()
            assert self.gate.wait(10), "test gate never opened"
        if self.failure is not None:
            raise self.failure
        result: Dict[str, Dict[str, Any]] = self._registry.list_repos()
        return result


def _in_thread(fn: Callable[[], Any]) -> Tuple[threading.Thread, List[Any]]:
    box: List[Any] = []

    def run() -> None:
        try:
            box.append(fn())
        except Exception as exc:  # noqa: BLE001 - the test inspects it
            box.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def test_stalled_registry_refresh_does_not_block_other_callers(
    fx: CidxMetaFixture,
) -> None:
    now: List[float] = [1000.0]
    registry = _GatedRegistry(fx.registry, free=1)
    universe = AliasUniverse(registry, ttl_seconds=60, clock=lambda: now[0])
    first = universe.snapshot()
    now[0] += 61
    try:
        leader, leader_box = _in_thread(universe.snapshot)
        assert registry.entered.wait(5)
        follower, follower_box = _in_thread(universe.snapshot)
        follower.join(2)
        assert not follower.is_alive(), "a stalled registry read blocked a caller"
        assert follower_box == [first]
        assert registry.reads == 2
    finally:
        registry.gate.set()
    leader.join(5)
    assert leader_box and leader_box[0].names == first.names


def test_first_load_waits_for_the_single_flight_read(fx: CidxMetaFixture) -> None:
    registry = _GatedRegistry(fx.registry, free=0)
    universe = AliasUniverse(registry)
    try:
        leader, leader_box = _in_thread(universe.snapshot)
        assert registry.entered.wait(5)
        follower, follower_box = _in_thread(universe.snapshot)
        follower.join(0.3)
        assert follower.is_alive(), "a caller without a snapshot must wait"
    finally:
        registry.gate.set()
    leader.join(5)
    follower.join(5)
    assert registry.reads == 1
    assert follower_box[0] is leader_box[0]
    assert CANARY in follower_box[0].names


def test_registry_failure_raises_and_never_yields_an_empty_universe(
    fx: CidxMetaFixture,
) -> None:
    registry = _GatedRegistry(fx.registry, free=0)
    registry.failure = ConnectionError("registry unavailable")
    universe = AliasUniverse(registry)
    try:
        leader, leader_box = _in_thread(universe.snapshot)
        assert registry.entered.wait(5)
        follower, follower_box = _in_thread(universe.snapshot)
        follower.join(0.3)
    finally:
        registry.gate.set()
    leader.join(5)
    follower.join(5)
    assert isinstance(leader_box[0], ConnectionError)
    assert isinstance(follower_box[0], RuntimeError)
    registry.failure = None
    assert CANARY in universe.snapshot().names


def test_alias_universe_rejects_a_non_positive_ttl(fx: CidxMetaFixture) -> None:
    with pytest.raises(ValueError):
        AliasUniverse(fx.registry, ttl_seconds=0)


def test_follower_raises_instead_of_stale_after_a_failed_refresh(
    fx: CidxMetaFixture,
) -> None:
    now: List[float] = [1000.0]
    registry = _GatedRegistry(fx.registry, free=1)
    universe = AliasUniverse(registry, ttl_seconds=60, clock=lambda: now[0])
    universe.snapshot()
    now[0] += 61
    registry.failure = ConnectionError("registry unavailable")
    registry.gate.set()
    with pytest.raises(ConnectionError):
        universe.snapshot()

    registry.gate.clear()
    registry.entered.clear()
    try:
        leader, _leader_box = _in_thread(universe.snapshot)
        assert registry.entered.wait(5)
        follower, follower_box = _in_thread(universe.snapshot)
        follower.join(2)
        assert not follower.is_alive()
        assert isinstance(follower_box[0], RuntimeError), follower_box
    finally:
        registry.gate.set()
    leader.join(5)


def test_follower_raises_once_the_snapshot_is_past_the_staleness_bound(
    fx: CidxMetaFixture,
) -> None:
    now: List[float] = [1000.0]
    registry = _GatedRegistry(fx.registry, free=1)
    universe = AliasUniverse(registry, ttl_seconds=60, clock=lambda: now[0])
    universe.snapshot()
    now[0] += 10_000
    try:
        leader, leader_box = _in_thread(universe.snapshot)
        assert registry.entered.wait(5)
        follower, follower_box = _in_thread(universe.snapshot)
        follower.join(2)
        assert not follower.is_alive()
        assert isinstance(follower_box[0], RuntimeError), follower_box
    finally:
        registry.gate.set()
    leader.join(5)
    assert CANARY in leader_box[0].names
