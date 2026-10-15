"""A golden HNSW orphan repair under a delegated IndexStore reaches every pod.

When a golden candidate's root IS the target its alias serves (a clone-path
target, i.e. a repo not refreshed since it was added -- ``.versioned``
snapshots are never candidates), the index there is a PUBLISHED version
every pod checks out. Repairing it in place would fix one pod. So the sweep:

* makes this pod hold the version the alias records (a stale copy swapped),
* repairs it, publishes the repair on top of exactly that version (the store
  refuses if anyone published since) and records the new version for the
  same target only if the alias still names it with the starting version;
* other pods holding the pre-repair copy (its marker names the old version)
  swap it on their next read.

Every other candidate -- a clone path the alias no longer serves (this pod's
build source; its repair is published by the next refresh, as locally),
``golden_temporal`` shards (pod-local), any candidate under the local store
-- is repaired in place exactly as before.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.services import index_materializer as im
from code_indexer.server.services.hnsw_orphan_sweep import scheduler as sweep
from code_indexer.server.services.hnsw_orphan_sweep.discovery import SweepCandidate
from code_indexer.server.services.hnsw_orphan_sweep.repair_executor import (
    SweepOutcome,
)
from code_indexer.server.startup import index_store_wiring
from code_indexer.utils.index_store import (
    INDEX_DIRNAME,
    INDEX_VERSION_FIELD,
    DelegatedIndexStore,
    LocalIndexStore,
    published_index_fields,
)
from tests.unit.server.index_store_fakes import FakeArtifacts

REPO = "repo"
ALIAS = f"{REPO}-global"
RELPATH = Path(INDEX_DIRNAME) / "index" / "coll" / "hnsw_index.bin"


class _GoldenManager:
    def __init__(self, golden_repos_dir: Path) -> None:
        self.golden_repos_dir = str(golden_repos_dir)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    artifacts = FakeArtifacts(tmp_path / "store")
    store = DelegatedIndexStore(artifacts)
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: store)
    monkeypatch.setattr(im, "run_fix_config", lambda target: None)
    golden = tmp_path / "pod" / "golden-repos"
    clone = golden / REPO  # the alias's target: added, never refreshed
    built = tmp_path / "pod-a" / REPO / INDEX_DIRNAME
    (built / "index" / "coll").mkdir(parents=True)
    (built / "index" / "coll" / "hnsw_index.bin").write_bytes(b"orphans")
    (built / "index" / "coll" / "collection_meta.json").write_text("{}")
    version = store.publish_as(REPO, built)
    aliases = AliasManager(str(golden / "aliases"))
    aliases.create_alias(
        ALIAS, str(clone), repo_name=REPO, extra=published_index_fields(REPO, version)
    )
    artifacts.calls.clear()
    return {
        "artifacts": artifacts,
        "store": store,
        "golden": golden,
        "clone": clone,
        "aliases": aliases,
        "v1": version,
    }


def _scheduler(world: Dict[str, Any], outcome: SweepOutcome) -> Any:
    instance: Any = sweep.HNSWOrphanRepairSweepScheduler.__new__(
        sweep.HNSWOrphanRepairSweepScheduler
    )
    instance._golden_repo_manager = _GoldenManager(world["golden"])
    instance._activated_repo_manager = None
    seen: List[bytes] = []

    def process(candidate: Any) -> SweepOutcome:
        path = Path(candidate.repo_root) / candidate.index_relpath
        seen.append(path.read_bytes())
        if outcome == SweepOutcome.REPAIRED:
            path.write_bytes(b"repaired")
        return outcome

    instance._process_fn = process
    instance.seen = seen
    return instance


def _candidate(root: Path, kind: str = "golden") -> SweepCandidate:
    return SweepCandidate(
        sort_key=f"{kind}:{REPO}:{RELPATH}",
        repo_root=root,
        index_relpath=RELPATH,
        kind=kind,
        alias=REPO,
    )


def _bin(root: Path) -> bytes:
    return (root / RELPATH).read_bytes()


def test_a_repair_of_the_served_target_is_published_and_recorded(
    world: Dict[str, Any],
) -> None:
    scheduler = _scheduler(world, SweepOutcome.REPAIRED)
    outcome = scheduler._process_one(_candidate(world["clone"]))
    assert outcome == SweepOutcome.REPAIRED
    assert scheduler.seen == [b"orphans"]  # the recorded version was repaired
    record = world["aliases"].read_record(ALIAS)
    new = world["artifacts"].current_version(REPO)
    assert new != world["v1"] and record[INDEX_VERSION_FIELD] == new
    assert ("publish", REPO, world["v1"], str(world["clone"] / INDEX_DIRNAME)) in (
        world["artifacts"].calls
    )
    assert (world["clone"] / im.CHECKED_OUT_MARKER).read_text() == new


def test_another_pod_swaps_its_pre_repair_copy(world: Dict[str, Any]) -> None:
    old_record = dict(world["aliases"].read_record(ALIAS))
    _scheduler(world, SweepOutcome.REPAIRED)._process_one(_candidate(world["clone"]))
    # pod C: holds the pre-repair version (its marker names v1)
    shutil.rmtree(world["clone"])
    im.ensure_published_index(world["clone"], old_record, store=world["store"])
    assert _bin(world["clone"]) == b"orphans"
    im.ensure_published_index(
        world["clone"], world["aliases"].read_record(ALIAS), store=world["store"]
    )
    assert _bin(world["clone"]) == b"repaired"


def test_a_clean_index_publishes_nothing(world: Dict[str, Any]) -> None:
    scheduler = _scheduler(world, SweepOutcome.CLEAN)
    assert scheduler._process_one(_candidate(world["clone"])) == SweepOutcome.CLEAN
    assert not any(c[0] == "publish" for c in world["artifacts"].calls)
    assert world["aliases"].read_record(ALIAS)[INDEX_VERSION_FIELD] == world["v1"]


def test_a_publish_since_the_starting_version_is_an_error(
    world: Dict[str, Any],
) -> None:
    scheduler = _scheduler(world, SweepOutcome.REPAIRED)
    other = world["golden"].parent / "other" / INDEX_DIRNAME
    (other / "index").mkdir(parents=True)
    world["store"].publish_as(REPO, other)  # a refresh published meanwhile
    assert scheduler._process_one(_candidate(world["clone"])) == SweepOutcome.ERROR
    assert world["aliases"].read_record(ALIAS)[INDEX_VERSION_FIELD] == world["v1"]


def test_an_alias_moved_meanwhile_keeps_the_newer_pointer(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = AliasManager.record_republished_index

    def swapped_first(
        self: Any, alias: str, target: str, base: str, fields: Any
    ) -> None:
        record = self.read_record(alias)
        record[INDEX_VERSION_FIELD] = "v-from-a-refresh"
        self.create_alias(alias, record["target_path"], extra=record)
        real(self, alias, target, base, fields)

    monkeypatch.setattr(AliasManager, "record_republished_index", swapped_first)
    scheduler = _scheduler(world, SweepOutcome.REPAIRED)
    assert scheduler._process_one(_candidate(world["clone"])) == SweepOutcome.ERROR
    assert (
        world["aliases"].read_record(ALIAS)[INDEX_VERSION_FIELD] == "v-from-a-refresh"
    )


def test_a_served_target_without_a_recorded_version_is_an_error(
    world: Dict[str, Any],
) -> None:
    world["aliases"].create_alias(ALIAS, str(world["clone"]), repo_name=REPO)
    (world["clone"] / RELPATH).parent.mkdir(parents=True)
    (world["clone"] / RELPATH).write_bytes(b"x")
    scheduler = _scheduler(world, SweepOutcome.REPAIRED)
    assert scheduler._process_one(_candidate(world["clone"])) == SweepOutcome.ERROR
    assert scheduler.seen == []


def test_an_unreachable_store_is_an_error_before_any_repair(
    world: Dict[str, Any],
) -> None:
    world["artifacts"].fail_checkout = True
    scheduler = _scheduler(world, SweepOutcome.REPAIRED)
    assert scheduler._process_one(_candidate(world["clone"])) == SweepOutcome.ERROR
    assert scheduler.seen == []


def test_process_one_is_fail_soft_when_published_golden_raises(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception inside _published_golden must not abort the sweep pass."""
    scheduler = _scheduler(world, SweepOutcome.REPAIRED)
    monkeypatch.setattr(
        type(scheduler),
        "_published_golden",
        lambda self, c: (_ for _ in ()).throw(RuntimeError("registry down")),
    )
    assert scheduler._process_one(_candidate(world["clone"])) == SweepOutcome.ERROR
    assert scheduler.seen == []


def test_a_build_source_the_alias_no_longer_serves_is_repaired_in_place(
    world: Dict[str, Any],
) -> None:
    snapshot = world["golden"] / ".versioned" / REPO / "v_200"
    world["aliases"].create_alias(
        ALIAS, str(snapshot), repo_name=REPO, extra=published_index_fields(REPO, "v9")
    )
    (world["clone"] / RELPATH).parent.mkdir(parents=True)
    (world["clone"] / RELPATH).write_bytes(b"scratch")
    scheduler = _scheduler(world, SweepOutcome.REPAIRED)
    assert scheduler._process_one(_candidate(world["clone"])) == SweepOutcome.REPAIRED
    assert scheduler.seen == [b"scratch"]
    assert world["artifacts"].calls == []


def test_temporal_shards_are_repaired_in_place(world: Dict[str, Any]) -> None:
    shard = world["golden"] / ".temporal" / REPO
    (shard / RELPATH).parent.mkdir(parents=True)
    (shard / RELPATH).write_bytes(b"temporal")
    scheduler = _scheduler(world, SweepOutcome.REPAIRED)
    candidate = _candidate(shard, kind="golden_temporal")
    assert scheduler._process_one(candidate) == SweepOutcome.REPAIRED
    assert world["artifacts"].calls == []


def test_local_store_repairs_in_place_without_reading_aliases(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        index_store_wiring, "server_index_store", lambda: LocalIndexStore()
    )
    monkeypatch.setattr(
        AliasManager, "read_record", lambda *a: pytest.fail("alias read locally")
    )
    (world["clone"] / RELPATH).parent.mkdir(parents=True)
    (world["clone"] / RELPATH).write_bytes(b"local")
    scheduler = _scheduler(world, SweepOutcome.REPAIRED)
    assert scheduler._process_one(_candidate(world["clone"])) == SweepOutcome.REPAIRED
    assert scheduler.seen == [b"local"] and _bin(world["clone"]) == b"repaired"


# ── record_republished_index ─────────────────────────────────────────────────


def test_recording_refuses_another_target_or_version(world: Dict[str, Any]) -> None:
    aliases = world["aliases"]
    fields = published_index_fields(REPO, "v2")
    with pytest.raises(RuntimeError, match="no longer points"):
        aliases.record_republished_index(ALIAS, "/elsewhere", world["v1"], fields)
    with pytest.raises(RuntimeError, match="records version"):
        aliases.record_republished_index(ALIAS, str(world["clone"]), "v0", fields)
    with pytest.raises(RuntimeError, match="no longer points"):
        aliases.record_republished_index("nope-global", "/x", "v", fields)
    aliases.record_republished_index(ALIAS, str(world["clone"]), world["v1"], fields)
    assert aliases.read_record(ALIAS)[INDEX_VERSION_FIELD] == "v2"


def _make_flaky_aliases(
    aliases: AliasManager, fail_count: int
) -> AliasManager:
    """Return an AliasManager whose record_republished_index fails
    *fail_count* times before succeeding (or never if fail_count >= 3)."""
    original = aliases.record_republished_index
    call_count = 0

    def flaky(*args: Any, **kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count <= fail_count:
            raise RuntimeError("transient registry error")
        return original(*args, **kwargs)

    aliases.record_republished_index = flaky  # type: ignore[assignment]
    return aliases


def test_record_retries_on_transient_metadata_failure(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient put failure is retried and the repair still succeeds."""
    (world["clone"] / RELPATH).parent.mkdir(parents=True)
    (world["clone"] / RELPATH).write_bytes(b"orphans")
    scheduler = _scheduler(world, SweepOutcome.REPAIRED)
    flaky = _make_flaky_aliases(world["aliases"], fail_count=2)
    monkeypatch.setattr(
        type(scheduler), "_golden_aliases", lambda self: flaky
    )
    assert scheduler._process_one(_candidate(world["clone"])) == SweepOutcome.REPAIRED
    assert world["aliases"].read_record(ALIAS)[INDEX_VERSION_FIELD] != world["v1"]


def test_record_permanent_failure_returns_error_after_retries(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three record attempts fail → ERROR, alias still at V1."""
    (world["clone"] / RELPATH).parent.mkdir(parents=True)
    (world["clone"] / RELPATH).write_bytes(b"orphans")
    scheduler = _scheduler(world, SweepOutcome.REPAIRED)
    flaky = _make_flaky_aliases(world["aliases"], fail_count=99)
    monkeypatch.setattr(
        type(scheduler), "_golden_aliases", lambda self: flaky
    )
    assert scheduler._process_one(_candidate(world["clone"])) == SweepOutcome.ERROR
    assert world["aliases"].read_record(ALIAS).get(INDEX_VERSION_FIELD) == world["v1"]


def test_record_ambiguous_success_detected_as_already_recorded(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The put committed but the call raised; on retry the alias already
    records our new version → treat as success, not ERROR."""
    (world["clone"] / RELPATH).parent.mkdir(parents=True)
    (world["clone"] / RELPATH).write_bytes(b"orphans")
    scheduler = _scheduler(world, SweepOutcome.REPAIRED)
    aliases = world["aliases"]
    original = aliases.record_republished_index
    call_count = 0

    def ambiguous(*args: Any, **kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            original(*args, **kwargs)
            raise RuntimeError("connection lost after commit")
        return original(*args, **kwargs)

    aliases.record_republished_index = ambiguous  # type: ignore[assignment]
    monkeypatch.setattr(type(scheduler), "_golden_aliases", lambda self: aliases)
    assert scheduler._process_one(_candidate(world["clone"])) == SweepOutcome.REPAIRED
    assert world["aliases"].read_record(ALIAS)[INDEX_VERSION_FIELD] != world["v1"]


def test_record_target_mismatch_does_not_retry(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The alias moved to a different target → ERROR immediately, no retry."""
    (world["clone"] / RELPATH).parent.mkdir(parents=True)
    (world["clone"] / RELPATH).write_bytes(b"orphans")
    scheduler = _scheduler(world, SweepOutcome.REPAIRED)
    aliases = world["aliases"]
    call_count = 0

    def target_moved(*args: Any, **kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError(f"Alias {ALIAS} no longer points at {world['clone']}")

    aliases.record_republished_index = target_moved  # type: ignore[assignment]
    monkeypatch.setattr(type(scheduler), "_golden_aliases", lambda self: aliases)
    assert scheduler._process_one(_candidate(world["clone"])) == SweepOutcome.ERROR
    assert call_count == 1, "should not retry deterministic target mismatch"


# ── version-tracking serving and the publisher's marker ─────────────────────


def test_an_unmarked_index_built_here_is_served_as_is(world: Dict[str, Any]) -> None:
    (world["clone"] / RELPATH).parent.mkdir(parents=True)
    (world["clone"] / RELPATH).write_bytes(b"built here")
    im.ensure_published_index(
        world["clone"], world["aliases"].read_record(ALIAS), store=world["store"]
    )
    assert _bin(world["clone"]) == b"built here"
    assert world["artifacts"].calls == []


def test_prune_never_removes_a_newer_snapshot_in_flight(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ns = world["golden"] / ".versioned" / REPO
    target, older, newer = ns / "v_200", ns / "v_100", ns / "v_300"
    for path in (older, newer):
        (path / INDEX_DIRNAME / "index").mkdir(parents=True)
        (path / im.CHECKED_OUT_MARKER).write_text("v")
    scheduled: List[str] = []
    monkeypatch.setattr(im, "_schedule_removal", lambda p: scheduled.append(str(p)))
    record = dict(world["aliases"].read_record(ALIAS), target_path=str(target))
    im.ensure_published_index(target, record, store=world["store"])
    assert scheduled == [str(older)]


def test_a_refresh_snapshot_carries_the_marker(world: Dict[str, Any]) -> None:
    from code_indexer.global_repos import refresh_scheduler as rs

    class _Snapshots:
        uses_flexclone = False

        def get_snapshot_path(self, alias: str, ts: str) -> str:
            return str(world["golden"] / ".versioned" / alias / f"v_{ts}")

    scheduler: Any = rs.RefreshScheduler.__new__(rs.RefreshScheduler)
    scheduler._index_store = world["store"]
    scheduler._snapshot_manager = _Snapshots()
    scheduler.resource_config = None
    scheduler._run_subprocess = lambda *a, **k: None
    scheduler._pending_meta_fingerprints = {}
    scheduler._pending_index_publications = {}
    scheduler._registry = MagicMock()
    source = world["golden"].parent / "src" / REPO
    (source / INDEX_DIRNAME / "index").mkdir(parents=True)
    snapshot = Path(scheduler._create_snapshot(ALIAS, str(source)))
    assert (snapshot / im.CHECKED_OUT_MARKER).read_text() == world[
        "artifacts"
    ].current_version(REPO)


def test_an_add_marks_the_version_it_published(world: Dict[str, Any]) -> None:
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoManager,
    )

    clone = world["golden"].parent / "adder" / REPO
    (clone / INDEX_DIRNAME / "index").mkdir(parents=True)
    manager = GoldenRepoManager.__new__(GoldenRepoManager)
    fields = manager._publish_added_index(str(clone))
    assert fields is not None
    assert (clone / im.CHECKED_OUT_MARKER).read_text() == fields[INDEX_VERSION_FIELD]


def test_a_branch_change_snapshot_marks_the_version_it_published(
    world: Dict[str, Any],
) -> None:
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoManager,
    )

    snapshot = world["golden"] / ".versioned" / REPO / "v_7"
    (snapshot / INDEX_DIRNAME / "index").mkdir(parents=True)
    manager = GoldenRepoManager.__new__(GoldenRepoManager)
    fields = manager._publish_index(str(world["clone"]), str(snapshot))
    assert fields is not None
    assert (snapshot / im.CHECKED_OUT_MARKER).read_text() == fields[INDEX_VERSION_FIELD]
    assert not (world["clone"] / im.CHECKED_OUT_MARKER).exists()


def test_a_migration_snapshot_marks_the_version_it_published(
    world: Dict[str, Any], tmp_path: Path
) -> None:
    from code_indexer.server.services.fleet_migration.snapshot_trigger import (
        trigger_post_consolidation_snapshot,
    )
    from tests.unit.server.services.test_unpublished_swaps_fail_loud import (
        _scheduler as _real_scheduler,
    )

    scheduler = _real_scheduler(tmp_path / "migration", world["store"])
    source = tmp_path / "migration" / "golden-repos" / REPO
    (source / INDEX_DIRNAME / "index").mkdir(parents=True)
    target = Path(trigger_post_consolidation_snapshot(scheduler, REPO, str(source)))
    assert (target / im.CHECKED_OUT_MARKER).read_text() == world[
        "artifacts"
    ].current_version(REPO)


def test_marking_refuses_fields_without_a_version(tmp_path: Path) -> None:
    with pytest.raises(im.PublishedIndexUnavailableError):
        im.mark_published_here(tmp_path, {})
    assert not (tmp_path / im.CHECKED_OUT_MARKER).exists()
