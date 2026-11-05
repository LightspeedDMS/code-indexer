"""Every pod sweeps its leftover IndexStore checkouts (delegated only).

Pinned over a pod directory tree:

* removed: a marked checkout of an activated copy the registry no longer has
  (or whose entry names another alias), a composite's members included, and a
  marked golden checkout no alias pointer names any more;
* kept: anything unmarked (built here, not checked out), anything registered
  or named, anything changed within the grace period, anything a query on
  this pod holds, anything that is not checkout state (and its directory);
* a registry / pointer read failure removes nothing and is reported;
* the sweep holds the target's checkout lock and re-checks under it;
* local store: nothing is read or removed, and no sweeper starts.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.server.services import checkout_sweep as cs
from code_indexer.server.services.index_materializer import (
    CHECKED_OUT_MARKER,
    IN_PROGRESS_MARKER,
    INCOMING_DIRNAME,
)
from code_indexer.server.startup import index_store_wiring
from code_indexer.utils.index_store import (
    INDEX_DIRNAME,
    INDEX_STORE_ALIAS_FIELD,
    DelegatedIndexStore,
    LocalIndexStore,
    activated_index_store_alias,
)
from tests.unit.server.index_store_fakes import FakeArtifacts

NOW = 1_800_000_000.0
OLD = NOW - 3600
GRACE = 900


class Registry:
    def __init__(self, entries: List[Dict[str, Any]]) -> None:
        self.entries = entries
        self.fail = False
        self.reads = 0

    def list_all_activated_repositories(self) -> List[Dict[str, Any]]:
        self.reads += 1
        if self.fail:
            raise ConnectionError("registry down")
        return self.entries


def _checkout(target: Path, *, when: float = OLD, marker: bool = True) -> Path:
    (target / INDEX_DIRNAME / "index").mkdir(parents=True)
    (target / INDEX_DIRNAME / "index" / "chunks.db").write_bytes(b"x")
    if marker:
        (target / CHECKED_OUT_MARKER).write_text("v1")
        os.utime(target / CHECKED_OUT_MARKER, (when, when))
    return target


@pytest.fixture
def pod(tmp_path: Path) -> SimpleNamespace:
    activated = tmp_path / "data" / "activated-repos"
    golden = tmp_path / "data" / "golden-repos"
    activated.mkdir(parents=True)
    golden.mkdir(parents=True)
    return SimpleNamespace(
        activated=activated,
        golden=golden,
        registry=Registry([]),
        pointers={},
        tracker=QueryTracker(),
        store=DelegatedIndexStore(FakeArtifacts(tmp_path / "store")),
        pointer_fail=False,
    )


def _sweeper(pod: SimpleNamespace, **kw: Any) -> cs.CheckoutSweeper:
    def pointers() -> Dict[str, Tuple[str, Optional[str]]]:
        if pod.pointer_fail:
            raise ConnectionError("registry down")
        return dict(pod.pointers)

    return cs.CheckoutSweeper(
        activated_repo_manager=pod.registry,
        activated_repos_dir=str(pod.activated),
        golden_repos_dir=str(pod.golden),
        query_tracker=pod.tracker,
        aliases_reader=pointers,
        store_provider=kw.get("store_provider", lambda: pod.store),
        grace_seconds=GRACE,
        clock=lambda: NOW,
    )


def _copy(pod: SimpleNamespace, user: str, alias: str) -> Path:
    return Path(pod.activated) / user / alias


# ── activated copies ────────────────────────────────────────────────────────


def test_an_unregistered_copys_checkout_is_removed(pod: SimpleNamespace) -> None:
    gone = _checkout(_copy(pod, "alice", "repo"))
    report = _sweeper(pod).sweep_once()
    assert report.removed == [str(gone)]
    assert not gone.exists()


def test_a_registered_copy_is_kept(pod: SimpleNamespace) -> None:
    kept = _checkout(_copy(pod, "alice", "repo"))
    pod.registry.entries = [
        {
            "username": "alice",
            "user_alias": "repo",
            INDEX_STORE_ALIAS_FIELD: activated_index_store_alias("alice", "repo"),
        }
    ]
    assert _sweeper(pod).sweep_once().removed == []
    assert (kept / INDEX_DIRNAME).is_dir()


def test_a_legacy_registered_copy_without_an_alias_is_kept(
    pod: SimpleNamespace,
) -> None:
    kept = _checkout(_copy(pod, "alice", "repo"))
    pod.registry.entries = [{"username": "alice", "user_alias": "repo"}]
    assert _sweeper(pod).sweep_once().removed == []
    assert kept.exists()


def test_an_entry_naming_another_alias_does_not_keep_the_checkout(
    pod: SimpleNamespace,
) -> None:
    gone = _checkout(_copy(pod, "alice", "repo"))
    pod.registry.entries = [
        {
            "username": "alice",
            "user_alias": "repo",
            INDEX_STORE_ALIAS_FIELD: activated_index_store_alias("bob", "repo"),
        }
    ]
    assert _sweeper(pod).sweep_once().removed == [str(gone)]


def test_another_users_registration_does_not_keep_it(pod: SimpleNamespace) -> None:
    gone = _checkout(_copy(pod, "alice", "repo"))
    pod.registry.entries = [{"username": "bob", "user_alias": "repo"}]
    assert _sweeper(pod).sweep_once().removed == [str(gone)]


def test_an_unmarked_directory_is_never_touched(pod: SimpleNamespace) -> None:
    built = _checkout(_copy(pod, "alice", "repo"), marker=False)
    snapshot = _checkout(pod.golden / ".versioned" / "gone" / "v_1", marker=False)
    report = _sweeper(pod).sweep_once()
    assert report == cs.CheckoutSweepReport(), "an unmarked target was a candidate"
    assert (built / INDEX_DIRNAME / "index" / "chunks.db").exists()
    assert (snapshot / INDEX_DIRNAME).exists()


def test_a_recent_checkout_is_kept(pod: SimpleNamespace) -> None:
    young = _checkout(_copy(pod, "alice", "repo"), when=NOW - GRACE + 5)
    report = _sweeper(pod).sweep_once()
    assert report.removed == [] and report.kept_recent == [str(young)]
    assert young.exists()


def test_a_checkout_in_use_by_a_query_is_kept_and_left_admittable(
    pod: SimpleNamespace,
) -> None:
    busy = _checkout(_copy(pod, "alice", "repo"))
    pod.tracker.increment_ref(str(busy))
    report = _sweeper(pod).sweep_once()
    assert report.kept_in_use == [str(busy)] and busy.exists()
    assert not pod.tracker.is_quiescing(str(busy))
    pod.tracker.decrement_ref(str(busy))
    assert _sweeper(pod).sweep_once().removed == [str(busy)]
    assert not pod.tracker.is_quiescing(str(busy))


def test_a_registry_failure_removes_nothing(pod: SimpleNamespace) -> None:
    kept = _checkout(_copy(pod, "alice", "repo"))
    pod.registry.fail = True
    report = _sweeper(pod).sweep_once()
    assert report.removed == [] and report.errors == ["activated: registry unreadable"]
    assert kept.exists()


def test_only_checkout_state_is_removed_never_other_content(
    pod: SimpleNamespace,
) -> None:
    target = _checkout(_copy(pod, "alice", "repo"))
    (target / IN_PROGRESS_MARKER).write_text("v")
    (target / INCOMING_DIRNAME).mkdir()
    (target / "README.md").write_text("not ours")
    _sweeper(pod).sweep_once()
    assert sorted(p.name for p in target.iterdir()) == ["README.md"]


def test_a_composites_members_go_with_it(pod: SimpleNamespace) -> None:
    composite = _checkout(_copy(pod, "alice", "combo"))
    member = _checkout(composite / "member-a")
    other = composite / "not-a-checkout"
    other.mkdir()
    (other / "file").write_text("keep")
    unmarked = _checkout(composite / "member-b", marker=False)
    _sweeper(pod).sweep_once()
    assert not member.exists()
    assert (other / "file").read_text() == "keep"
    assert (unmarked / INDEX_DIRNAME).is_dir(), "removed state no checkout wrote"
    assert not (composite / INDEX_DIRNAME).exists()


def test_no_metadata_file_means_truly_unregistered(pod: SimpleNamespace) -> None:
    """Without a metadata file, the copy is genuinely gone and removed."""
    gone = _checkout(_copy(pod, "alice", "repo"))
    assert not (pod.activated / "alice" / "repo_metadata.json").exists()
    report = _sweeper(pod).sweep_once()
    assert report.removed == [str(gone)]
    assert not gone.exists()


def test_a_corrupt_metadata_file_keeps_the_checkout(pod: SimpleNamespace) -> None:
    """A metadata file that the registry could not parse should NOT cause the
    sweep to treat the copy as unregistered and delete its checkout."""
    target = _checkout(_copy(pod, "alice", "repo"))
    metadata = pod.activated / "alice" / "repo_metadata.json"
    metadata.write_text("{corrupt json!!")
    report = _sweeper(pod).sweep_once()
    assert report.removed == []
    assert str(target) in report.kept_recent
    assert (target / INDEX_DIRNAME).is_dir()


def test_the_trash_directory_is_not_a_user(pod: SimpleNamespace) -> None:
    trash = _checkout(pod.activated / ".trash" / "x")
    assert _sweeper(pod).sweep_once().removed == []
    assert trash.exists()


# ── golden checkouts ────────────────────────────────────────────────────────


def test_a_removed_golden_repos_checkouts_are_removed(pod: SimpleNamespace) -> None:
    snapshot = _checkout(pod.golden / ".versioned" / "gone" / "v_1")
    clone = _checkout(pod.golden / "gone")
    report = _sweeper(pod).sweep_once()
    assert sorted(report.removed) == sorted([str(snapshot), str(clone)])


def test_a_golden_repo_with_a_pointer_keeps_its_checkouts(
    pod: SimpleNamespace,
) -> None:
    snapshot = _checkout(pod.golden / ".versioned" / "live" / "v_1")
    clone = _checkout(pod.golden / "live")
    pod.pointers = {"live-global": (str(pod.golden / ".versioned/live/v_9"), None)}
    assert _sweeper(pod).sweep_once().removed == []
    assert snapshot.exists() and clone.exists()


def test_a_path_named_by_any_pointer_is_kept(pod: SimpleNamespace) -> None:
    shared = _checkout(pod.golden / ".versioned" / "renamed" / "v_1")
    pod.pointers = {"other-global": (str(pod.golden / "x"), str(shared))}
    assert _sweeper(pod).sweep_once().removed == []


def test_unreadable_pointers_remove_no_golden_checkout(pod: SimpleNamespace) -> None:
    kept = _checkout(pod.golden / ".versioned" / "gone" / "v_1")
    pod.pointer_fail = True
    report = _sweeper(pod).sweep_once()
    assert report.errors == ["golden: alias pointers unreadable"] and kept.exists()


def test_the_aliases_directory_is_not_a_repository(pod: SimpleNamespace) -> None:
    aliases = _checkout(pod.golden / "aliases")
    assert _sweeper(pod).sweep_once().removed == []
    assert aliases.exists()


def test_the_default_pointer_reader_is_the_alias_manager(
    pod: SimpleNamespace,
) -> None:
    from code_indexer.global_repos.alias_manager import AliasManager

    AliasManager(str(pod.golden / "aliases")).create_alias(
        "live-global", str(pod.golden / "live"), repo_name="live"
    )
    clone = _checkout(pod.golden / "live")
    sweeper = cs.CheckoutSweeper(
        activated_repo_manager=pod.registry,
        activated_repos_dir=str(pod.activated),
        golden_repos_dir=str(pod.golden),
        query_tracker=None,
        store_provider=lambda: pod.store,
        grace_seconds=GRACE,
        clock=lambda: NOW,
    )
    assert sweeper.sweep_once().removed == []
    assert clone.exists()


# ── locking ─────────────────────────────────────────────────────────────────


def test_removal_holds_the_checkout_lock_and_rechecks_under_it(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _checkout(_copy(pod, "alice", "repo"))
    locked: List[str] = []

    @contextlib.contextmanager
    def lock(path: Path) -> Iterator[None]:
        locked.append(str(path))
        # a checkout finishing just before the lock was granted
        os.utime(path / CHECKED_OUT_MARKER, (NOW, NOW))
        yield

    monkeypatch.setattr(cs, "exclusive_checkout", lock)
    report = _sweeper(pod).sweep_once()
    assert locked == [str(target)]
    assert report.removed == [] and report.kept_recent == [str(target)]
    assert target.exists()


def test_removal_happens_inside_the_lock(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _checkout(_copy(pod, "alice", "repo"))
    events: List[str] = []
    real_rmtree = shutil.rmtree

    @contextlib.contextmanager
    def lock(path: Path) -> Iterator[None]:
        events.append("lock")
        yield
        events.append("unlock")

    monkeypatch.setattr(cs, "exclusive_checkout", lock)

    def rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        events.append("rm")
        real_rmtree(path)

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    _sweeper(pod).sweep_once()
    assert events[0] == "lock" and events[-1] == "unlock" and "rm" in events
    assert not target.exists()


# ── local store ─────────────────────────────────────────────────────────────


def test_the_local_store_reads_and_removes_nothing(pod: SimpleNamespace) -> None:
    kept = _checkout(_copy(pod, "alice", "repo"))
    golden = _checkout(pod.golden / ".versioned" / "gone" / "v_1")
    pod.pointer_fail = True
    report = _sweeper(pod, store_provider=LocalIndexStore).sweep_once()
    assert report == cs.CheckoutSweepReport()
    assert pod.registry.reads == 0
    assert kept.exists() and golden.exists()


def test_no_sweeper_starts_under_the_local_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        index_store_wiring, "server_index_store", lambda: LocalIndexStore()
    )
    app = SimpleNamespace(state=SimpleNamespace())
    assert index_store_wiring.start_checkout_sweeper(app, object()) is None
    assert app.state.checkout_sweeper is None


def test_a_sweeper_starts_and_stops_under_a_delegated_store(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: pod.store)
    manager = SimpleNamespace(
        activated_repos_dir=str(pod.activated),
        list_all_activated_repositories=lambda: [],
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            activated_repo_manager=manager,
            golden_repos_dir=str(pod.golden),
            query_tracker=pod.tracker,
        )
    )
    config = SimpleNamespace(
        index_checkout_sweep_interval_seconds=3600,
        index_checkout_sweep_grace_seconds=60,
    )
    sweeper = index_store_wiring.start_checkout_sweeper(app, config)
    assert sweeper is app.state.checkout_sweeper and sweeper is not None
    assert sweeper._grace == 60 and sweeper._thread is not None
    index_store_wiring.stop_checkout_sweeper(app)
    assert sweeper._thread is None


def test_one_failing_removal_does_not_abort_the_pass(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = _checkout(_copy(pod, "alice", "repo-a"))
    fail = _checkout(_copy(pod, "alice", "repo-b"))
    real_remove = cs._remove_pod_state

    def exploding_remove(target: Path) -> None:
        if target == fail:
            raise PermissionError("locked by another process")
        real_remove(target)

    monkeypatch.setattr(cs, "_remove_pod_state", exploding_remove)
    report = _sweeper(pod).sweep_once()
    assert str(ok) in report.removed
    assert any("removal failed" in e for e in report.errors)


def test_a_permission_error_in_subdirs_is_caught(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = _checkout(_copy(pod, "alice", "repo"))
    real_iterdir = Path.iterdir

    def failing_iterdir(self: Path) -> Any:
        if self == pod.activated:
            raise PermissionError("no access")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", failing_iterdir)
    report = _sweeper(pod).sweep_once()
    assert report.removed == []
    assert kept.exists()


def test_the_loop_runs_passes_and_survives_a_failing_one(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    sweeper = _sweeper(pod)
    calls: List[int] = []

    def flaky() -> cs.CheckoutSweepReport:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        sweeper._stop.set()
        return cs.CheckoutSweepReport()

    monkeypatch.setattr(sweeper, "sweep_once", flaky)
    sweeper.start(0.01)
    assert sweeper._thread is not None
    sweeper._thread.join(timeout=5)
    assert len(calls) == 2


# ── pins, readers in any process, another purge's mark ──────────────────────


def test_a_pinned_leftover_is_kept(pod: SimpleNamespace) -> None:
    from code_indexer.server.services import served_index_cache as sic

    target = _checkout(_copy(pod, "alice", "repo"))
    with sic.pinned_served_index(target):
        report = _sweeper(pod).sweep_once()
    assert report.removed == [] and report.kept_in_use == [str(target)]
    assert (target / INDEX_DIRNAME).is_dir()


def test_a_leftover_a_reader_in_another_process_holds_is_kept(
    pod: SimpleNamespace,
) -> None:
    import fcntl

    from code_indexer.server.services import served_index_cache as sic

    target = _checkout(_copy(pod, "alice", "repo"))
    # A shared hold on its own open file description is what another
    # process's read (or pin) looks like: no QueryTracker ref, no pin here.
    fd = os.open(str(sic.readers_lock_path(target)), os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_SH)
    try:
        report = _sweeper(pod).sweep_once()
    finally:
        os.close(fd)
    assert report.removed == [] and report.kept_in_use == [str(target)]
    assert _sweeper(pod).sweep_once().removed == [str(target)]


def test_a_leftover_another_purge_is_quiescing_is_kept_and_its_mark_stays(
    pod: SimpleNamespace,
) -> None:
    target = _checkout(_copy(pod, "alice", "repo"))
    pod.tracker.mark_quiescing(str(target))
    report = _sweeper(pod).sweep_once()
    assert report.removed == [] and report.kept_in_use == [str(target)]
    assert pod.tracker.is_quiescing(str(target))


def test_the_sweep_clears_only_its_own_mark(pod: SimpleNamespace) -> None:
    target = _checkout(_copy(pod, "alice", "repo"))
    assert _sweeper(pod).sweep_once().removed == [str(target)]
    assert not pod.tracker.is_quiescing(str(target))


@pytest.mark.parametrize("interval", [0, -1])
def test_a_non_positive_sweep_interval_is_refused(
    pod: SimpleNamespace, interval: float
) -> None:
    sweeper = _sweeper(pod)
    with pytest.raises(ValueError, match="positive"):
        sweeper.start(interval)
    assert sweeper._thread is None


# ── EVO-64616: no residue (emptied directories, lock files) ─────────────────


def _locks(directory: Path) -> List[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".lock"))


def test_a_removed_copy_leaves_no_directory_lock_files_or_empty_user_dir(
    pod: SimpleNamespace,
) -> None:
    gone = _checkout(_copy(pod, "admin", "e64616-fd"))

    assert _sweeper(pod).sweep_once().removed == [str(gone)]

    assert not gone.exists()
    assert not gone.parent.exists(), "the emptied <user>/ dir was kept"


def test_a_user_dir_with_other_copies_is_kept(pod: SimpleNamespace) -> None:
    gone = _checkout(_copy(pod, "admin", "gone"))
    (gone.parent / "kept_metadata.json").write_text("{}")

    _sweeper(pod).sweep_once()

    assert not gone.exists()
    assert sorted(p.name for p in gone.parent.iterdir()) == ["kept_metadata.json"]


def test_residue_an_earlier_pass_left_is_removed(pod: SimpleNamespace) -> None:
    user_dir = pod.activated / "admin"
    (user_dir / "e64616-fd").mkdir(parents=True)  # emptied, rmdir had failed
    (user_dir / ".e64616-fd.checkout.lock").write_text("")
    (user_dir / ".e64616-fd.readers.lock").write_text("")
    (user_dir / ".vanished.checkout.lock").write_text("")  # target long gone

    report = _sweeper(pod).sweep_once()

    assert sorted(report.removed) == sorted(
        [str(user_dir / "e64616-fd"), str(user_dir / "vanished")]
    )
    assert not user_dir.exists()


def test_residue_of_a_registered_copy_is_kept(pod: SimpleNamespace) -> None:
    user_dir = pod.activated / "admin"
    (user_dir / "live").mkdir(parents=True)
    (user_dir / ".live.checkout.lock").write_text("")
    pod.registry.entries = [{"username": "admin", "user_alias": "live"}]

    _sweeper(pod).sweep_once()

    assert (user_dir / "live").is_dir()
    assert _locks(user_dir) == [".live.checkout.lock"]


def test_residue_a_reader_holds_is_kept(pod: SimpleNamespace) -> None:
    import fcntl

    user_dir = pod.activated / "admin"
    (user_dir / "busy").mkdir(parents=True)
    (user_dir / ".busy.checkout.lock").write_text("")
    readers = user_dir / ".busy.readers.lock"
    readers.write_text("")
    fd = os.open(readers, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        _sweeper(pod).sweep_once()
    finally:
        os.close(fd)

    assert (user_dir / "busy").is_dir()
    assert _locks(user_dir) == [".busy.checkout.lock", ".busy.readers.lock"]


def test_residue_whose_target_holds_files_is_kept(pod: SimpleNamespace) -> None:
    user_dir = pod.activated / "admin"
    (user_dir / "odd").mkdir(parents=True)
    (user_dir / "odd" / "unexpected.txt").write_text("x")
    (user_dir / ".odd.checkout.lock").write_text("")

    _sweeper(pod).sweep_once()

    assert (user_dir / "odd" / "unexpected.txt").is_file()
    assert _locks(user_dir) == [".odd.checkout.lock"]


def test_a_removed_golden_repo_leaves_no_versioned_residue(
    pod: SimpleNamespace,
) -> None:
    snapshot = _checkout(pod.golden / ".versioned" / "gone" / "v_1")
    old = pod.golden / ".versioned" / "gone"
    (old / ".v_0.checkout.lock").write_text("")  # an older pass's leftover

    _sweeper(pod).sweep_once()

    assert not snapshot.exists()
    assert not old.exists(), "the emptied .versioned/<repo>/ was kept"
    assert (pod.golden / ".versioned").is_dir()


def test_a_lock_file_deleted_under_a_waiter_is_reopened(tmp_path: Path) -> None:
    """flock_live: a waiter granted the lock on a deleted file locks the
    path's CURRENT file instead, so two holders never lock different files."""
    import fcntl
    import threading

    from code_indexer.server.services.index_materializer import flock_live

    path = tmp_path / ".t.checkout.lock"
    first = flock_live(path, fcntl.LOCK_EX)
    got: List[int] = []
    waiter = threading.Thread(
        target=lambda: got.append(flock_live(path, fcntl.LOCK_EX))
    )
    waiter.start()
    waiter.join(timeout=0.3)
    assert waiter.is_alive(), "the waiter must be blocked on the held lock"
    path.unlink()  # the sweep deletes the lock file while holding it
    fcntl.flock(first, fcntl.LOCK_UN)
    os.close(first)
    waiter.join(timeout=10)
    try:
        assert got and os.fstat(got[0]).st_ino == os.stat(path).st_ino
    finally:
        for fd in got:
            os.close(fd)
