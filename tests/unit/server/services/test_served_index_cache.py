"""A pod's served-index checkouts stay inside its disk (IndexStore, delegated).

Pinned over a real pod directory tree and a fake artifact service holding real
trees, with the cache installed into the readers' hooks
(``index_materializer``, ``temporal_index_store``):

* LRU: the least recently served checkout goes first; access is recorded on a
  serve (in memory, plus a throttled stamp that survives a restart);
* watermarks: nothing happens under the high one; over it, eviction runs down
  to the low one -- as total bytes and as free-disk percentage;
* never evicted: pinned (a build in progress), held by a query, leased by a
  snapshot reader, its repository's write lock held, served within the idle
  floor, its checkout lock held elsewhere, or not a checkout at all;
* eviction renames before it deletes: an open reader keeps its files, a reader
  racing the eviction waits for the lock and checks the index out again;
* the next serve after an eviction checks it out again, transparently;
* admission: room is made BEFORE a checkout; one checkout larger than the cache
  may hold is refused loudly, removed, and not downloaded again;
* every kind: golden clone path, golden snapshot, activated copy, composite
  (with its members, as one), temporal shard root;
* observability: eviction log line, counters on the health/metrics surface;
* the local store: no cache, no hook, no file -- today's behaviour.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional, cast

import pytest

from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.snapshot_reader_lease import (
    SnapshotReaderLease,
    ensure_primary_lease_directory,
)
from code_indexer.server.services import checkout_sweep as cs
from code_indexer.server.services import index_materializer as im
from code_indexer.server.services import served_index_cache as sic
from code_indexer.server.services import temporal_index_store as tis
from code_indexer.server.startup import index_store_wiring
from code_indexer.server.utils.config_manager import ServerResourceConfig
from code_indexer.utils.index_store import (
    INDEX_DIRNAME,
    DelegatedIndexStore,
    LocalIndexStore,
    published_index_fields,
    published_temporal_fields,
    temporal_index_store_alias,
)
from tests.unit.server.index_store_fakes import FakeArtifacts

T0 = 1_800_000_000.0
IDLE = 600.0
KB = 1000


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


class FakeDisk:
    """A disk of ``total`` bytes whose use is ``base`` plus every file under
    ``root`` -- so evicting really frees space."""

    def __init__(self, root: Path, total: int, base: int = 0) -> None:
        self.root, self.total, self.base = root, total, base

    def __call__(self) -> sic.DiskUsage:
        used = self.base + sic.tree_bytes(self.root)
        return sic.DiskUsage(self.total, max(self.total - used, 0))


def _payload(dir_: Path, size: int, *, index: bool = True) -> Path:
    built = dir_ / INDEX_DIRNAME
    (built / "index").mkdir(parents=True)
    (built / "index" / "hnsw.bin").write_bytes(b"x" * size)
    if not index:
        shutil.rmtree(built / "index")
        (built / "proxy.json").write_bytes(b"p" * size)
    return built


@pytest.fixture
def pod(tmp_path: Path) -> Iterator[SimpleNamespace]:
    artifacts = FakeArtifacts(tmp_path / "store")
    store = DelegatedIndexStore(artifacts)
    data = tmp_path / "pod" / "data"
    golden = data / "golden-repos"
    activated = data / "activated-repos"
    golden.mkdir(parents=True)
    activated.mkdir(parents=True)
    clock = Clock()
    world = SimpleNamespace(
        tmp=tmp_path,
        artifacts=artifacts,
        store=store,
        golden=golden,
        activated=activated,
        clock=clock,
        tracker=QueryTracker(),
        locked=set(),
        cache=None,
    )
    yield world
    sic.install_served_index_cache(None)


def _cache(
    pod: SimpleNamespace,
    *,
    max_bytes: int = 0,
    low_bytes: int = 0,
    min_free: float = 0.0,
    target_free: float = 0.0,
    idle: float = IDLE,
    disk: Optional[Callable[[], sic.DiskUsage]] = None,
    lease_root: Optional[Path] = None,
) -> sic.ServedIndexCache:
    cache = sic.ServedIndexCache(
        activated_repos_dir=pod.activated,
        golden_repos_dir=pod.golden,
        limits=sic.ServedIndexCacheLimits(
            max_bytes=max_bytes,
            low_bytes=low_bytes,
            min_free_percent=min_free,
            target_free_percent=target_free,
            min_idle_seconds=idle,
        ),
        query_tracker=pod.tracker,
        build_locked=lambda repo: repo in pod.locked,
        lease_root=lease_root,
        disk_usage=disk or FakeDisk(pod.tmp / "pod", total=10**12),
        clock=pod.clock,
    )
    sic.install_served_index_cache(cache)
    pod.cache = cache
    return cache


def _publish(pod: SimpleNamespace, alias: str, size: int, **kw: Any) -> str:
    scratch = pod.tmp / "scratch" / alias.replace("/", "_")
    shutil.rmtree(scratch, ignore_errors=True)
    built = _payload(scratch, size, **kw)
    return str(
        pod.artifacts.publish(alias, pod.artifacts.current_version(alias), str(built))
    )


def _snapshot(pod: SimpleNamespace, repo: str, ts: int = 1) -> Path:
    golden: Path = pod.golden
    return golden / ".versioned" / repo / f"v_{ts}"


def _serve_golden(pod: SimpleNamespace, target: Path, repo: str) -> None:
    version = pod.artifacts.current_version(repo)
    im.ensure_published_index(
        target,
        {"target_path": str(target), **published_index_fields(repo, version)},
        store=pod.store,
        fix_config=lambda _p: None,
    )


def _golden(pod: SimpleNamespace, repo: str, size: int = 10 * KB, ts: int = 1) -> Path:
    """A golden snapshot this pod checked out, served at the clock's now."""
    if pod.artifacts.current_version(repo) is None:
        _publish(pod, repo, size)
    target = _snapshot(pod, repo, ts)
    with _unlimited(pod):
        _serve_golden(pod, target, repo)
    _age_marker(target, pod.clock.now)
    return target


@contextlib.contextmanager
def _unlimited(pod: SimpleNamespace) -> Iterator[None]:
    """Set-up: check out with no cache in the way (no admission, no counts)."""
    sic.install_served_index_cache(None)
    try:
        yield
    finally:
        sic.install_served_index_cache(pod.cache)


def _age_marker(target: Path, when: float) -> None:
    os.utime(target / im.CHECKED_OUT_MARKER, (when, when))


def _limit(cache: sic.ServedIndexCache, max_bytes: int) -> None:
    """Tighten (or, with 0, lift) the byte watermarks of a running cache."""
    cache.limits = sic.ServedIndexCacheLimits(
        max_bytes=max_bytes,
        low_bytes=max_bytes,
        min_free_percent=0.0,
        target_free_percent=0.0,
        min_idle_seconds=IDLE,
    )


def _served(target: Path) -> bool:
    return (target / INDEX_DIRNAME / "index").is_dir()


def _checkouts(pod: SimpleNamespace) -> List[Any]:
    return [c for c in pod.artifacts.calls if c[0] == "checkout"]


def _later(pod: SimpleNamespace, seconds: float = IDLE + 1) -> None:
    pod.clock.now += seconds


# ── LRU order ────────────────────────────────────────────────────────────────


def test_the_least_recently_served_checkout_is_evicted_first(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod, max_bytes=25 * KB, low_bytes=20 * KB)
    a = _golden(pod, "a")
    _later(pod, 10)
    b = _golden(pod, "b")
    _later(pod, 10)
    c = _golden(pod, "c")
    _later(pod)
    _serve_golden(pod, a, "a")  # a is now the most recently served
    _later(pod)
    report = cache.run_pass()
    assert report.evicted == [str(b)]
    assert _served(a) and not _served(b) and _served(c)


def test_access_survives_a_restart_through_the_stamp(pod: SimpleNamespace) -> None:
    _cache(pod, max_bytes=25 * KB, low_bytes=20 * KB)
    a = _golden(pod, "a")
    b = _golden(pod, "b")
    c = _golden(pod, "c")
    _later(pod)
    _serve_golden(pod, a, "a")
    os.utime(a / sic.LAST_SERVED_STAMP, (pod.clock.now, pod.clock.now))
    _later(pod)
    restarted = _cache(pod, max_bytes=25 * KB, low_bytes=20 * KB)  # no memory
    report = restarted.run_pass()
    assert str(a) not in report.evicted
    assert len(report.evicted) == 1 and _served(a)
    assert {str(b), str(c)} & set(report.evicted)


def test_the_stamp_is_written_at_most_once_per_interval(pod: SimpleNamespace) -> None:
    cache = _cache(pod)
    assert cache.stamp_interval() == IDLE / 3  # a third of the idle floor
    _limit(cache, 0)
    cache.limits = sic.ServedIndexCacheLimits(
        max_bytes=0,
        low_bytes=0,
        min_free_percent=0,
        target_free_percent=0,
        min_idle_seconds=3600,
    )
    assert cache.stamp_interval() == sic.STAMP_INTERVAL_SECONDS  # capped
    a = _golden(pod, "a")
    _serve_golden(pod, a, "a")
    stamp = a / sic.LAST_SERVED_STAMP
    assert stamp.is_file()
    os.utime(stamp, (1.0, 1.0))
    pod.clock.now += sic.STAMP_INTERVAL_SECONDS - 1
    _serve_golden(pod, a, "a")
    assert stamp.stat().st_mtime == 1.0
    pod.clock.now += 1
    _serve_golden(pod, a, "a")
    assert stamp.stat().st_mtime > 1.0


def test_no_stamp_is_written_into_a_directory_that_is_not_a_checkout(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod)
    built = pod.golden / "built-here"
    _payload(built, KB)
    assert cache.note_hit(built)
    assert not (built / sic.LAST_SERVED_STAMP).exists()


# ── watermarks ───────────────────────────────────────────────────────────────


def test_nothing_is_evicted_under_the_high_watermark(pod: SimpleNamespace) -> None:
    cache = _cache(pod, max_bytes=31 * KB, low_bytes=10 * KB)
    targets = [_golden(pod, name) for name in ("a", "b", "c")]
    _later(pod)
    assert cache.run_pass().evicted == []
    assert all(_served(t) for t in targets)


def test_over_the_high_watermark_it_evicts_down_to_the_low_one(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod, max_bytes=35 * KB, low_bytes=15 * KB)
    targets = []
    for name in ("a", "b", "c", "d"):
        targets.append(_golden(pod, name))
        _later(pod, 1)
    _later(pod)
    report = cache.run_pass()
    assert report.evicted == [str(targets[0]), str(targets[1]), str(targets[2])]
    assert cache.cache_bytes() <= 15 * KB
    assert _served(targets[3])


def test_at_exactly_the_high_watermark_nothing_is_evicted(
    pod: SimpleNamespace,
) -> None:
    _cache(pod)
    a = _golden(pod, "a")
    size = sic.tree_bytes(a / INDEX_DIRNAME)
    cache = _cache(pod, max_bytes=size, low_bytes=size)
    _later(pod)
    assert cache.run_pass().evicted == []


def test_low_free_disk_evicts_until_the_target_free_percentage(
    pod: SimpleNamespace,
) -> None:
    disk = FakeDisk(pod.tmp / "pod", total=100 * KB, base=50 * KB)
    cache = _cache(pod, min_free=15.0, target_free=25.0, disk=disk)
    targets = []
    for name in ("a", "b", "c", "d"):
        targets.append(_golden(pod, name))
        _later(pod, 1)
    # 50 KB base + 4 x 10 KB = 90 KB used: 10% free < 15%
    _later(pod)
    report = cache.run_pass()
    assert report.evicted == [str(targets[0]), str(targets[1])]
    assert disk().free * 100 >= 25 * disk.total


def test_free_disk_at_the_floor_is_not_over(pod: SimpleNamespace) -> None:
    _cache(pod)
    a = _golden(pod, "a")
    used = sic.tree_bytes(pod.tmp / "pod")
    disk = FakeDisk(pod.tmp / "pod", total=used * 4, base=used * 2)  # 25% free
    cache = _cache(pod, min_free=25.0, target_free=30.0, disk=disk)
    _later(pod)
    assert cache.run_pass().evicted == [] and _served(a)


def test_the_limits_are_validated() -> None:
    sic.ServedIndexCacheLimits()
    sic.ServedIndexCacheLimits(max_bytes=0, low_bytes=0)
    with pytest.raises(ValueError):
        sic.ServedIndexCacheLimits(max_bytes=10, low_bytes=11)
    with pytest.raises(ValueError):
        sic.ServedIndexCacheLimits(max_bytes=10, low_bytes=0)
    with pytest.raises(ValueError):
        sic.ServedIndexCacheLimits(max_bytes=-1, low_bytes=0)
    with pytest.raises(ValueError):
        sic.ServedIndexCacheLimits(min_free_percent=20, target_free_percent=10)
    with pytest.raises(ValueError):
        sic.ServedIndexCacheLimits(min_free_percent=100, target_free_percent=100)
    with pytest.raises(ValueError):
        sic.ServedIndexCacheLimits(min_idle_seconds=-1)
    with pytest.raises(ValueError):  # it guards untracked in-flight queries
        sic.ServedIndexCacheLimits(min_idle_seconds=0)


def test_the_defaults_fit_a_20_gib_disk() -> None:
    limits = sic.ServedIndexCacheLimits()
    assert limits.max_bytes == 14 * 1024**3 and limits.low_bytes == 12 * 1024**3
    assert limits.min_free_percent == 15.0 and limits.target_free_percent == 25.0
    config = ServerResourceConfig()
    assert index_store_wiring.build_served_index_cache_limits(config) == limits
    assert config.served_index_cache_interval_seconds == 60


# ── protection ───────────────────────────────────────────────────────────────


def _two_over(pod: SimpleNamespace) -> sic.ServedIndexCache:
    """Two 10 KB checkouts, a 15 KB high watermark: one must go."""
    return _cache(pod, max_bytes=15 * KB, low_bytes=10 * KB)


def test_a_pinned_checkout_is_never_evicted(pod: SimpleNamespace) -> None:
    cache = _two_over(pod)
    a = _golden(pod, "a")
    _later(pod, 1)
    b = _golden(pod, "b")
    _later(pod)
    with sic.pinned_served_index(a):
        report = cache.run_pass()
    assert report.skipped[str(a)] == sic.SKIP_PINNED
    assert report.evicted == [str(b)] and _served(a)
    assert not sic.is_pinned(a)


def test_pins_are_reentrant(pod: SimpleNamespace) -> None:
    with sic.pinned_served_index(pod.golden / "x"):
        with sic.pinned_served_index(pod.golden / "x"):
            pass
        assert sic.is_pinned(pod.golden / "x")
    assert not sic.is_pinned(pod.golden / "x")


def test_a_checkout_a_query_holds_is_never_evicted(pod: SimpleNamespace) -> None:
    cache = _two_over(pod)
    a = _golden(pod, "a")
    _later(pod)
    _golden(pod, "b")
    _later(pod)
    with pod.tracker.track_query(str(a)):
        report = cache.run_pass()
    assert report.skipped[str(a)] == sic.SKIP_IN_USE and _served(a)
    assert not pod.tracker.is_quiescing(str(a))


def test_a_query_arriving_during_eviction_is_refused_admission(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _two_over(pod)
    a = _golden(pod, "a")
    _later(pod, 1)
    _golden(pod, "b")
    _later(pod)
    seen: List[bool] = []
    real = sic.retire_pod_state

    def retire(target: Path) -> Optional[Path]:
        seen.append(pod.tracker.try_increment_ref_if_not_quiescing(str(target)))
        return real(target)

    monkeypatch.setattr(sic, "retire_pod_state", retire)
    assert cache.run_pass().evicted == [str(a)]
    assert seen == [False]
    assert not pod.tracker.is_quiescing(str(a))


def test_a_query_admitted_under_the_lock_keeps_the_checkout(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)
    real = cache._protected
    monkeypatch.setattr(
        cache,
        "_protected",
        lambda entry: (pod.tracker.increment_ref(str(a)), real(entry))[1],
    )
    # the query is admitted after the first check, before quiescing
    monkeypatch.setattr(pod.tracker, "get_ref_count", _counts_after_first(pod))
    report = cache.run_pass()
    assert report.skipped[str(a)] == sic.SKIP_IN_USE and _served(a)
    assert not pod.tracker.is_quiescing(str(a))


def _counts_after_first(pod: SimpleNamespace) -> Callable[[str], int]:
    calls: List[str] = []

    def count(path: str) -> int:
        calls.append(path)
        return 0 if len(calls) == 1 else 1

    return count


def test_a_snapshot_with_a_live_reader_lease_is_never_evicted(
    pod: SimpleNamespace,
) -> None:
    lease_root = pod.golden / "cidx-meta"
    ensure_primary_lease_directory(lease_root)
    cache = _cache(pod, max_bytes=15 * KB, low_bytes=10 * KB, lease_root=lease_root)
    a = _golden(pod, "a")
    _later(pod, 1)
    b = _golden(pod, "b")
    _later(pod)
    lease = SnapshotReaderLease(str(a), 3600, lease_root=lease_root)
    lease.acquire()
    report = cache.run_pass()
    assert report.skipped[str(a)] == sic.SKIP_LEASED
    assert report.evicted == [str(b)]
    lease.release()


def test_an_unreadable_lease_state_keeps_the_snapshot(pod: SimpleNamespace) -> None:
    cache = _cache(
        pod, max_bytes=5 * KB, low_bytes=5 * KB, lease_root=pod.golden / "none"
    )
    a = _golden(pod, "a")
    _later(pod)
    assert cache.run_pass().skipped[str(a)] == sic.SKIP_LEASED


def test_a_repository_whose_write_lock_is_held_is_never_evicted(
    pod: SimpleNamespace,
) -> None:
    cache = _two_over(pod)
    a = _golden(pod, "a")
    _later(pod, 1)
    b = _golden(pod, "b")
    _later(pod)
    pod.locked.add("a")
    report = cache.run_pass()
    assert report.skipped[str(a)] == sic.SKIP_BUILDING
    assert report.evicted == [str(b)]


def test_an_unreadable_write_lock_keeps_the_checkout(pod: SimpleNamespace) -> None:
    def broken(repo: str) -> bool:
        raise ConnectionError("lock store down")

    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    cache._build_locked = broken
    a = _golden(pod, "a")
    _later(pod)
    assert cache.run_pass().skipped[str(a)] == sic.SKIP_BUILDING and _served(a)


def test_a_recently_served_checkout_is_never_evicted(pod: SimpleNamespace) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    pod.clock.now += IDLE - 1
    report = cache.run_pass()
    assert report.skipped[str(a)] == sic.SKIP_RECENT and _served(a)
    assert not report.satisfied
    pod.clock.now += 1
    assert cache.run_pass().evicted == [str(a)]


def test_a_checkout_whose_lock_is_held_is_skipped_not_waited_for(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)
    holding, release = threading.Event(), threading.Event()

    def hold() -> None:
        with im.exclusive_checkout(a):
            holding.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    holding.wait(5)
    try:
        report = cache.run_pass()
    finally:
        release.set()
        thread.join(5)
    assert report.skipped[str(a)] == sic.SKIP_BUSY and _served(a)


def test_a_lock_another_process_holds_is_skipped(pod: SimpleNamespace) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)
    lock_path = a.parent / f".{a.name}.checkout.lock"
    ready_r, ready_w = os.pipe()
    done_r, done_w = os.pipe()
    pid = os.fork()
    if pid == 0:  # the child only holds the lock until told to stop
        fd = os.open(str(lock_path), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(ready_w, b"x")
        os.read(done_r, 1)
        os._exit(0)
    try:
        assert os.read(ready_r, 1) == b"x"
        report = cache.run_pass()
    finally:
        os.write(done_w, b"x")
        os.waitpid(pid, 0)
        for fd in (ready_r, ready_w, done_r, done_w):
            os.close(fd)
    assert report.skipped[str(a)] == sic.SKIP_BUSY and _served(a)


def test_a_directory_this_pod_built_without_publishing_is_never_a_candidate(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod, max_bytes=1, low_bytes=1)
    built = _snapshot(pod, "mine")
    _payload(built, 10 * KB)  # an unpublished build: no checkout marker
    _later(pod)
    report = cache.run_pass()
    assert report.evicted == [] and report.skipped == {} and _served(built)


def test_an_in_progress_checkout_is_not_a_candidate(pod: SimpleNamespace) -> None:
    cache = _cache(pod, max_bytes=1, low_bytes=1)
    target = _snapshot(pod, "half")
    _payload(target, 10 * KB)
    (target / im.IN_PROGRESS_MARKER).write_text("v")
    _later(pod)
    assert cache.run_pass().evicted == [] and _served(target)


def test_a_checkout_gone_by_the_time_it_is_locked_is_skipped(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)
    real = cache._protected

    def vanish(entry: sic.ServedCheckout) -> Optional[str]:
        (a / im.CHECKED_OUT_MARKER).unlink()
        return real(entry)

    monkeypatch.setattr(cache, "_protected", vanish)
    assert cache.run_pass().skipped[str(a)] == sic.SKIP_GONE
    assert _served(a)


# ── rename, then delete ──────────────────────────────────────────────────────


def test_eviction_renames_the_index_aside_before_deleting_it(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)
    deleted: List[Dict[str, Any]] = []
    real_rmtree = shutil.rmtree

    def rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        deleted.append(
            {
                "path": Path(path),
                "served": _served(a),
                "marker": (a / im.CHECKED_OUT_MARKER).exists(),
                "lock_free": _lock_free(a),
            }
        )
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    cache.run_pass()
    (removed,) = [d for d in deleted if d["path"].name.startswith(sic.EVICTED_PREFIX)]
    assert removed["path"].parent == a
    assert not removed["served"] and not removed["marker"]
    assert not any(p.name.startswith(sic.EVICTED_PREFIX) for p in a.iterdir())


def test_the_index_goes_before_the_marker_that_vouches_for_it(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader that trusts an unmarked index (one built here) must never see
    an index whose marker is already gone: the index is renamed first."""
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)
    at_rename: List[bool] = []
    real_rename = os.rename

    def rename(src: Any, dst: Any) -> None:
        if Path(src) == a / INDEX_DIRNAME:
            at_rename.append((a / im.CHECKED_OUT_MARKER).is_file())
        real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    assert cache.run_pass().evicted == [str(a)]
    assert at_rename == [True]
    assert not (a / im.CHECKED_OUT_MARKER).exists()


def _lock_free(target: Path) -> bool:
    with im.try_exclusive_checkout(target) as got:
        return got


def test_a_reader_with_open_files_keeps_reading_them(pod: SimpleNamespace) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)
    with open(a / INDEX_DIRNAME / "index" / "hnsw.bin", "rb") as reader:
        assert cache.run_pass().evicted == [str(a)]
        assert reader.read() == b"x" * 10 * KB


def test_a_reader_racing_the_eviction_waits_and_checks_out_again(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)
    retiring, resume = threading.Event(), threading.Event()
    real = sic.retire_pod_state

    def slow_retire(target: Path) -> Optional[Path]:
        retiring.set()
        resume.wait(5)
        return real(target)

    monkeypatch.setattr(sic, "retire_pod_state", slow_retire)
    evictor = threading.Thread(target=cache.run_pass)
    evictor.start()
    assert retiring.wait(5)
    # The reader's fast check still sees an index -- and must not trust it.
    assert _served(a) and not cache.note_hit(a)
    _limit(cache, 0)  # the reader's own checkout is admitted
    reader_done = threading.Event()

    def read() -> None:
        _serve_golden(pod, a, "a")
        reader_done.set()

    reader = threading.Thread(target=read)
    reader.start()
    assert not reader_done.wait(0.3)  # waiting for the checkout lock
    resume.set()
    evictor.join(5)
    reader.join(5)
    assert reader_done.is_set() and _served(a)
    assert len(_checkouts(pod)) == 2


# ── re-checkout ──────────────────────────────────────────────────────────────


def test_an_evicted_checkout_is_checked_out_again_on_its_next_serve(
    pod: SimpleNamespace,
) -> None:
    cache = _two_over(pod)
    a = _golden(pod, "a")
    _later(pod, 1)
    b = _golden(pod, "b")
    _later(pod)
    assert cache.run_pass().evicted == [str(a)]
    assert not _served(a) and a.is_dir()
    _serve_golden(pod, a, "a")  # makes room: b is now the coldest
    assert _served(a) and (a / im.CHECKED_OUT_MARKER).is_file()
    assert (a / INDEX_DIRNAME / "index" / "hnsw.bin").read_bytes() == b"x" * 10 * KB
    assert not _served(b)
    assert len(_checkouts(pod)) == 3


def test_eviction_closes_this_processes_cached_handles(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)
    closed: List[str] = []
    from code_indexer.server.cache import snapshot_cache_invalidation as sci

    monkeypatch.setattr(
        sci,
        "invalidate_snapshot_caches",
        lambda path, **kw: closed.append(str(path)),
    )
    cache.run_pass()
    assert closed == [str(a)]


# ── admission ────────────────────────────────────────────────────────────────


def test_room_is_made_before_a_checkout_that_would_cross_the_high_watermark(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _cache(pod, max_bytes=25 * KB, low_bytes=20 * KB)
    a = _golden(pod, "a")
    _later(pod, 1)
    b = _golden(pod, "b")
    _later(pod)
    _publish(pod, "c", 10 * KB)
    events: List[str] = []
    real_checkout = pod.artifacts.checkout

    def checkout(*args: Any, **kwargs: Any) -> Any:
        events.append(f"checkout a={_served(a)} b={_served(b)}")
        return real_checkout(*args, **kwargs)

    monkeypatch.setattr(pod.artifacts, "checkout", checkout)
    c = _snapshot(pod, "c")
    _serve_golden(pod, c, "c")
    assert events == ["checkout a=False b=True"]
    assert _served(c) and cache.evictions == 1


def test_an_admission_never_evicts_the_target_it_admits(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod, max_bytes=15 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)
    _publish(pod, "a", 10 * KB)  # a newer version: a is replaced in place
    _serve_golden(pod, a, "a")
    assert _served(a) and cache.evictions == 0


def test_one_checkout_larger_than_the_cache_is_refused_and_not_retried(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    _publish(pod, "huge", 10 * KB)
    target = _snapshot(pod, "huge")
    with pytest.raises(sic.ServedIndexTooLargeError, match="served_index_cache"):
        _serve_golden(pod, target, "huge")
    assert not _served(target) and not (target / im.CHECKED_OUT_MARKER).exists()
    assert cache.refusals == 1
    with pytest.raises(sic.ServedIndexTooLargeError):
        _serve_golden(pod, target, "huge")
    assert len(_checkouts(pod)) == 1  # not downloaded again
    assert isinstance(
        sic.ServedIndexTooLargeError("x"), im.PublishedIndexUnavailableError
    )


def test_a_new_version_of_a_refused_index_is_tried_again(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    _publish(pod, "huge", 10 * KB)
    target = _snapshot(pod, "huge")
    with pytest.raises(sic.ServedIndexTooLargeError):
        _serve_golden(pod, target, "huge")
    _publish(pod, "huge", 2 * KB)
    _serve_golden(pod, target, "huge")
    assert _served(target) and str(target) not in cache._refused


def test_the_single_checkout_ceiling_counts_the_free_disk_floor(
    pod: SimpleNamespace,
) -> None:
    disk = FakeDisk(pod.tmp / "pod", total=100 * KB)
    cache = _cache(pod, min_free=95.0, target_free=95.0, disk=disk)
    assert cache.single_checkout_ceiling() == 5 * KB
    _publish(pod, "big", 6 * KB)
    with pytest.raises(sic.ServedIndexTooLargeError):
        _serve_golden(pod, _snapshot(pod, "big"), "big")
    both = _cache(
        pod,
        max_bytes=3 * KB,
        low_bytes=3 * KB,
        min_free=95.0,
        target_free=95.0,
        disk=disk,
    )
    assert both.single_checkout_ceiling() == 3 * KB
    assert _cache(pod).single_checkout_ceiling() is None


def test_a_checkout_exactly_at_the_ceiling_is_admitted(pod: SimpleNamespace) -> None:
    _publish(pod, "fit", 5 * KB)
    probe = _cache(pod)
    target = _snapshot(pod, "fit")
    _serve_golden(pod, target, "fit")
    size = sic.tree_bytes(target / INDEX_DIRNAME)
    probe.run_pass()
    shutil.rmtree(target)
    _cache(pod, max_bytes=size, low_bytes=size)
    _serve_golden(pod, target, "fit")
    assert _served(target)


def test_an_unknown_size_is_estimated_from_the_cached_ones(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod)
    assert cache._expected_size("nothing") == 0
    _golden(pod, "a", size=10 * KB)
    _golden(pod, "b", size=20 * KB)
    cache.run_pass()
    mean = (cache.cache_bytes()) // 2
    assert cache._expected_size("new") == mean
    assert cache._expected_size(str(_snapshot(pod, "a"))) < mean


def test_a_known_size_is_remembered_after_eviction(pod: SimpleNamespace) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a", size=10 * KB)
    _later(pod)
    size = sic.tree_bytes(a / INDEX_DIRNAME)
    cache.run_pass()
    assert cache._expected_size(str(a)) == size


# ── every kind ───────────────────────────────────────────────────────────────


def test_a_golden_base_clone_checkout_is_never_evicted(
    pod: SimpleNamespace,
) -> None:
    """EVO-64616: ``golden-repos/<repo>`` itself is the refresh's incremental
    base (and, with the golden-repos mount on, shared by every pod): never a
    cache entry, even checked out, marked, idle and over the limit."""
    cache = _cache(pod)
    _publish(pod, "clone", 10 * KB)
    target = pod.golden / "clone"
    _serve_golden(pod, target, "clone")
    assert (target / im.CHECKED_OUT_MARKER).is_file()
    _limit(cache, 5 * KB)
    _later(pod)
    assert cache.run_pass().evicted == []
    assert _served(target)
    assert cache.cache_bytes() == 0


def test_a_base_clone_entry_is_skipped_at_eviction_time(
    pod: SimpleNamespace,
) -> None:
    """The guard behind discovery: an entry naming a base clone is refused
    by ``_protected`` whatever put it on the list."""
    cache = _cache(pod)
    entry = sic.ServedCheckout(sic.KIND_GOLDEN, pod.golden / "clone", "clone", "clone")
    assert cache._protected(entry) == sic.SKIP_BASE_CLONE
    snapshot = sic.ServedCheckout(
        sic.KIND_GOLDEN,
        _snapshot(pod, "clone", 7),
        "clone@v_7",
        "clone",
        versioned=True,
    )
    assert cache._protected(snapshot) is None


def _serve_activated(pod: SimpleNamespace, target: Path, alias: str, **kw: Any) -> None:
    version = pod.artifacts.current_version(alias)
    im.ensure_current_index(
        target,
        published_index_fields(alias, version),
        store=pod.store,
        fix_config=lambda _p: None,
        **kw,
    )


def test_an_activated_copy_is_evicted_and_served_again(pod: SimpleNamespace) -> None:
    cache = _cache(pod)
    _publish(pod, "activated.alice", 10 * KB)
    target = pod.activated / "alice" / "repo"
    _serve_activated(pod, target, "activated.alice")
    _limit(cache, 5 * KB)
    _later(pod)
    report = cache.run_pass()
    assert report.evicted == [str(target)]
    assert not _served(target)
    _limit(cache, 0)
    _serve_activated(pod, target, "activated.alice")
    assert _served(target)


def test_a_composite_is_evicted_with_its_members_as_one(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod, max_bytes=15 * KB, low_bytes=15 * KB)
    root = pod.activated / "alice" / "combo"
    _publish(pod, "activated.combo", KB, index=False)
    _serve_activated(pod, root, "activated.combo", require_index=False)
    members = []
    for name in ("m1", "m2"):
        _publish(pod, f"activated.combo/{name}", 10 * KB)
        member = root / name
        _serve_activated(pod, member, f"activated.combo/{name}")
        members.append(member)
    (entry,) = sic.discover_served_checkouts(pod.activated, None)
    assert entry.parts == tuple(members)
    _later(pod)
    report = cache.run_pass()
    assert report.evicted == [str(root)]
    assert not (root / INDEX_DIRNAME).exists()
    assert not any(_served(m) for m in members)
    _serve_activated(pod, root, "activated.combo", require_index=False)
    _serve_activated(pod, members[0], "activated.combo/m1")
    assert _served(members[0])


def test_a_composite_member_held_by_a_build_keeps_the_whole_composite(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod)
    root = pod.activated / "alice" / "combo"
    _publish(pod, "activated.combo", KB, index=False)
    _serve_activated(pod, root, "activated.combo", require_index=False)
    _publish(pod, "activated.combo/m1", 10 * KB)
    _serve_activated(pod, root / "m1", "activated.combo/m1")
    _limit(cache, 5 * KB)
    _later(pod)
    with sic.pinned_served_index(root / "m1"):
        assert cache.run_pass().skipped[str(root)] == sic.SKIP_PINNED
    assert _served(root / "m1")


def test_a_member_being_admitted_never_evicts_its_composite(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod, max_bytes=12 * KB, low_bytes=1 * KB)
    root = pod.activated / "alice" / "combo"
    _publish(pod, "activated.combo", KB, index=False)
    _serve_activated(pod, root, "activated.combo", require_index=False)
    _publish(pod, "activated.combo/m1", 6 * KB)
    _serve_activated(pod, root / "m1", "activated.combo/m1")
    _later(pod)
    _publish(pod, "activated.combo/m2", 6 * KB)
    _serve_activated(pod, root / "m2", "activated.combo/m2")
    assert cache.evictions == 0 and _served(root / "m1") and _served(root / "m2")


def _temporal_doc(pod: SimpleNamespace, repo: str) -> Dict[str, Any]:
    alias = temporal_index_store_alias(repo)
    return published_temporal_fields(alias, pod.artifacts.current_version(alias))


def _publish_temporal(pod: SimpleNamespace, repo: str, size: int) -> None:
    alias = temporal_index_store_alias(repo)
    shards = pod.tmp / "scratch" / f"temporal-{repo}"
    shutil.rmtree(shards, ignore_errors=True)
    (shards / "shard").mkdir(parents=True)
    (shards / "shard" / "data.bin").write_bytes(b"t" * size)
    pod.artifacts.publish(alias, pod.artifacts.current_version(alias), str(shards))


def _temporal_root(pod: SimpleNamespace, repo: str) -> Path:
    golden: Path = pod.golden
    return golden / sic.TEMPORAL_DIRNAME / repo


def test_a_temporal_shard_root_is_evicted_and_served_again(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod)
    _publish_temporal(pod, "repo", 10 * KB)
    root = _temporal_root(pod, "repo")
    tis.ensure_temporal_root(root, _temporal_doc(pod, "repo"), "repo", store=pod.store)
    _limit(cache, 5 * KB)
    assert root.is_symlink() and (root / "shard" / "data.bin").is_file()
    (entry,) = sic.discover_served_checkouts(None, pod.golden)
    assert entry.kind == sic.KIND_TEMPORAL and entry.repo == "repo"
    assert cache.run_pass().evicted == []  # fresh
    _later(pod)
    assert cache.run_pass().evicted == [str(root)]
    assert not root.exists() and not root.is_symlink()
    assert not sic.temporal_marker(root).exists()
    assert [p.name for p in root.parent.iterdir() if not p.name.endswith(".lock")] == []
    _limit(cache, 0)
    tis.ensure_temporal_root(root, _temporal_doc(pod, "repo"), "repo", store=pod.store)
    assert (root / "shard" / "data.bin").read_bytes() == b"t" * 10 * KB


def test_a_temporal_root_that_is_a_plain_directory_is_evicted(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    root = _temporal_root(pod, "legacy")
    (root / "shard").mkdir(parents=True)
    (root / "shard" / "data.bin").write_bytes(b"t" * 10 * KB)
    sic.temporal_marker(root).write_text("v1")
    os.utime(sic.temporal_marker(root), (T0, T0))
    _later(pod)
    assert cache.run_pass().evicted == [str(root)]
    assert not root.exists()


def test_a_temporal_root_under_a_refresh_is_never_evicted(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod)
    _publish_temporal(pod, "repo", 10 * KB)
    root = _temporal_root(pod, "repo")
    tis.ensure_temporal_root(root, _temporal_doc(pod, "repo"), "repo", store=pod.store)
    _limit(cache, 5 * KB)
    _later(pod)
    pod.locked.add("repo")
    assert cache.run_pass().skipped[str(root)] == sic.SKIP_BUILDING


def test_a_temporal_checkout_larger_than_the_cache_is_refused(
    pod: SimpleNamespace,
) -> None:
    _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    _publish_temporal(pod, "repo", 10 * KB)
    root = _temporal_root(pod, "repo")
    with pytest.raises(sic.ServedIndexTooLargeError):
        tis.ensure_temporal_root(
            root, _temporal_doc(pod, "repo"), "repo", store=pod.store
        )
    assert not root.exists() and not sic.temporal_marker(root).exists()


# ── observability ───────────────────────────────────────────────────────────


def test_an_eviction_is_logged_with_alias_size_and_reason(
    pod: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "agent-router")
    size = sic.tree_bytes(a / INDEX_DIRNAME)
    _later(pod)
    with caplog.at_level(logging.INFO, logger=sic.__name__):
        cache.run_pass()
    (line,) = [r.getMessage() for r in caplog.records if "evicted" in r.getMessage()]
    assert "agent-router@v_1" in line and f"{size} bytes" in line
    assert "reason=watermark" in line


def test_hits_misses_and_evictions_are_counted(pod: SimpleNamespace) -> None:
    cache = _cache(pod)
    _publish(pod, "a", 10 * KB)
    a = _snapshot(pod, "a")
    _serve_golden(pod, a, "a")  # miss
    _serve_golden(pod, a, "a")  # hit
    _serve_golden(pod, a, "a")  # hit
    _limit(cache, 5 * KB)
    _later(pod)
    size = sic.tree_bytes(a / INDEX_DIRNAME)
    cache.run_pass()
    stats = cache.stats()
    assert stats["hits"] == 2 and stats["misses"] == 1
    assert stats["hit_ratio"] == pytest.approx(2 / 3)
    assert stats["evictions"] == 1 and stats["evicted_bytes"] == size
    assert stats["entries"] == 0 and stats["cache_bytes"] == 0
    assert stats["max_bytes"] == 5 * KB and stats["disk_total_bytes"] == 10**12
    assert stats["last_pass_at"] == pod.clock.now


def test_the_health_surface_reports_the_cache_only_when_one_runs(
    pod: SimpleNamespace,
) -> None:
    from code_indexer.server.routers import inline_misc

    assert inline_misc._served_index_cache_stats() is None
    cache = _cache(pod)
    _publish(pod, "a", KB)
    _serve_golden(pod, _snapshot(pod, "a"), "a")
    cache.run_pass()
    stats = inline_misc._served_index_cache_stats()
    assert stats is not None and stats["entries"] == 1 and stats["misses"] == 1


# ── wiring, background pass, leftovers ──────────────────────────────────────


def _eventually(check: Callable[[], bool]) -> bool:
    done = threading.Event()
    for _ in range(500):
        if check():
            return True
        done.wait(0.01)
    return False


def _app(pod: SimpleNamespace) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(
            activated_repo_manager=SimpleNamespace(activated_repos_dir=pod.activated),
            golden_repos_dir=str(pod.golden),
            query_tracker=pod.tracker,
            global_lifecycle_manager=SimpleNamespace(
                refresh_scheduler=SimpleNamespace(
                    is_write_locked=lambda repo: repo in pod.locked
                )
            ),
        )
    )


def test_the_local_store_installs_no_cache_and_touches_nothing(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_store_wiring, "server_index_store", LocalIndexStore)
    app = _app(pod)
    assert (
        index_store_wiring.start_served_index_cache(app, ServerResourceConfig()) is None
    )
    assert app.state.served_index_cache is None
    assert sic.active_served_index_cache() is None
    target = _snapshot(pod, "local")
    _payload(target, KB)
    before = sorted(str(p) for p in pod.tmp.rglob("*"))
    im.ensure_published_index(target, None, store=LocalIndexStore())
    im.ensure_current_index(target, None, store=LocalIndexStore())
    tis.ensure_temporal_root(target, None, "local", store=LocalIndexStore())
    assert sorted(str(p) for p in pod.tmp.rglob("*")) == before
    assert not any(t.name == "served-index-cache" for t in threading.enumerate())


def test_a_delegated_store_starts_and_stops_the_cache(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: pod.store)
    app = _app(pod)
    config = ServerResourceConfig()
    config.served_index_cache_interval_seconds = 3600
    started = index_store_wiring.start_served_index_cache(app, config)
    assert isinstance(started, sic.ServedIndexCache)
    cache = started
    try:
        assert cache is app.state.served_index_cache is sic.active_served_index_cache()
        assert cache._thread is not None
        assert _eventually(lambda: cache.last_pass_at is not None)
        assert cache._lease_root == pod.golden / "cidx-meta"
        pod.locked.add("x")
        assert cache._build_locked("x") and not cache._build_locked("y")
    finally:
        index_store_wiring.stop_served_index_cache(app)
    assert cache._thread is None and sic.active_served_index_cache() is None


def test_invalid_watermarks_fail_the_start(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: pod.store)
    config = ServerResourceConfig()
    config.served_index_cache_low_bytes = config.served_index_cache_max_bytes + 1
    with pytest.raises(ValueError):
        index_store_wiring.start_served_index_cache(_app(pod), config)


def test_no_write_lock_reader_means_nothing_is_building(pod: SimpleNamespace) -> None:
    app = _app(pod)
    app.state.global_lifecycle_manager = None
    assert index_store_wiring._golden_write_locked(app)("a") is False


def test_a_failing_pass_does_not_stop_the_loop(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _cache(pod)
    calls: List[int] = []
    done = threading.Event()

    def run_pass() -> sic.EvictionReport:
        calls.append(1)
        if len(calls) >= 3:
            done.set()
        raise RuntimeError("boom")

    monkeypatch.setattr(cache, "run_pass", run_pass)
    cache.start(0.01)
    try:
        assert done.wait(5)
    finally:
        cache.stop()
    cache.start(0.01)
    cache.start(0.01)  # idempotent
    cache.stop()


def test_interrupted_evictions_are_removed_at_start(pod: SimpleNamespace) -> None:
    leftovers = [
        _snapshot(pod, "a") / f"{sic.EVICTED_PREFIX}1",
        pod.golden / "clone" / f"{sic.EVICTED_PREFIX}2",
        pod.activated / "alice" / "repo" / f"{sic.EVICTED_PREFIX}3",
        pod.activated / "alice" / "combo" / "m1" / f"{sic.EVICTED_PREFIX}4",
        pod.golden / sic.TEMPORAL_DIRNAME / f".repo{sic.TEMPORAL_EVICTED_INFIX}5",
    ]
    kept = pod.activated / "alice" / "repo" / "src"
    for path in leftovers + [kept]:
        (path / "f").mkdir(parents=True)
    assert sic.purge_leftovers(pod.activated, pod.golden) == len(leftovers)
    assert not any(p.exists() for p in leftovers) and kept.exists()


# ── shared with the leftover sweep ───────────────────────────────────────────


def test_the_sweep_uses_the_same_discovery_and_removal() -> None:
    assert cs.iter_activated_checkouts is sic.iter_activated_checkouts
    assert cs.iter_golden_checkouts is sic.iter_golden_checkouts
    assert cs.retire_pod_state is sic.retire_pod_state
    assert set(sic.POD_STATE_FILES) | set(sic.POD_STATE_DIRS) | {INDEX_DIRNAME} == set(
        cs.POD_STATE_NAMES
    )


def test_discovery_finds_every_kind_and_nothing_else(pod: SimpleNamespace) -> None:
    def mark(target: Path) -> Path:
        _payload(target, 1)
        (target / im.CHECKED_OUT_MARKER).write_text("v")
        return target

    mark(pod.golden / "clone")
    snap = mark(_snapshot(pod, "repo", 7))
    _payload(pod.golden / "built", 1)  # unmarked
    mark(pod.golden / "aliases")  # the alias store is never a repository
    copy = mark(pod.activated / "alice" / "repo")
    mark(pod.activated / ".trash" / "gone")
    root = _temporal_root(pod, "repo")
    root.mkdir(parents=True)
    sic.temporal_marker(root).write_text("v")
    found = {
        (e.kind, e.label, e.key)
        for e in sic.discover_served_checkouts(pod.activated, pod.golden)
    }
    assert found == {
        # EVO-64616: the marked base clone ``clone`` is never an entry.
        (sic.KIND_GOLDEN, "repo@v_7", str(snap)),
        (sic.KIND_ACTIVATED, "alice/repo", str(copy)),
        (sic.KIND_TEMPORAL, "repo (temporal)", str(root)),
    }


# ── try_exclusive_checkout ───────────────────────────────────────────────────


def test_try_exclusive_checkout_takes_a_free_lock_and_releases_it(
    tmp_path: Path,
) -> None:
    target = tmp_path / "t"
    with im.try_exclusive_checkout(target) as got:
        assert got
        with im.try_exclusive_checkout(target) as again:
            assert not again  # same thread, not re-entrant
    with im.try_exclusive_checkout(target) as got:
        assert got
    with im.exclusive_checkout(target):
        pass


# ── builds in place are pinned at their call sites ──────────────────────────


def test_an_activated_reindex_job_is_pinned_for_its_whole_run(
    tmp_path: Path,
) -> None:
    from code_indexer.server.services.activated_repo_index_manager import (
        ActivatedRepoIndexManager,
    )

    seen: List[bool] = []
    target = tmp_path / "alice" / "repo"

    def run(*args: Any) -> Dict[str, Any]:
        seen.append(sic.is_pinned(target))
        return {"success": True, "args": args}

    fake = SimpleNamespace(_run_indexing_job=run)
    result = ActivatedRepoIndexManager._execute_indexing_job(
        cast(Any, fake),
        "repo",
        str(target),
        ["semantic"],
        False,
        owner="alice",
    )
    assert seen == [True] and not sic.is_pinned(target)
    assert result["args"] == ("repo", str(target), ["semantic"], False, "alice", None)


def test_a_branch_delta_reindex_is_pinned_from_serve_to_publish(
    tmp_path: Path,
) -> None:
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    target = tmp_path / "alice" / "repo"
    seen: List[str] = []
    fake = SimpleNamespace(
        _index_manager=SimpleNamespace(
            run_branch_delta_index=lambda path, cancel_check=None: seen.append(
                f"index {sic.is_pinned(path)}"
            )
        ),
        logger=logging.getLogger("test"),
        serve_index_here=lambda user, alias: seen.append(
            f"serve {sic.is_pinned(target)}"
        ),
        publish_index=lambda user, alias: seen.append(
            f"publish {sic.is_pinned(target)}"
        ),
    )
    ActivatedRepoManager._run_branch_delta_index(
        cast(Any, fake),
        str(target),
        "repo",
        publish_for="alice",
    )
    assert seen == ["serve True", "index True", "publish True"]
    assert not sic.is_pinned(target)


def test_an_hnsw_orphan_repair_is_pinned(tmp_path: Path) -> None:
    from code_indexer.server.services.hnsw_orphan_sweep import scheduler as hs

    target = tmp_path / "golden" / ".versioned" / "repo" / "v_1"
    candidate = SimpleNamespace(repo_root=target, sort_key="repo")
    seen: List[bool] = []

    def dispatched(c: Any) -> Any:
        seen.append(sic.is_pinned(target))
        return hs.SweepOutcome.CLEAN

    fake = SimpleNamespace(_process_dispatched=dispatched)
    outcome = hs.HNSWOrphanRepairSweepScheduler._process_one(
        cast(Any, fake),
        candidate,
    )
    assert outcome == hs.SweepOutcome.CLEAN and seen == [True]
    assert not sic.is_pinned(target)


def test_room_is_made_before_an_activated_checkout(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _two_over(pod)
    a = _golden(pod, "a")
    _later(pod)
    _publish(pod, "activated.alice", 10 * KB)
    events: List[bool] = []
    real_checkout = pod.artifacts.checkout

    def checkout(*args: Any, **kwargs: Any) -> Any:
        events.append(_served(a))
        return real_checkout(*args, **kwargs)

    monkeypatch.setattr(pod.artifacts, "checkout", checkout)
    _serve_activated(pod, pod.activated / "alice" / "repo", "activated.alice")
    assert events == [False]  # a went first: 10 KB + 10 KB > 15 KB
    assert cache.evictions == 1


def test_an_activated_checkout_larger_than_the_cache_is_refused(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    _publish(pod, "activated.alice", 10 * KB)
    target = pod.activated / "alice" / "repo"
    with pytest.raises(sic.ServedIndexTooLargeError):
        _serve_activated(pod, target, "activated.alice")
    assert not _served(target) and cache.refusals == 1 and cache.misses == 1


def test_room_is_made_before_a_temporal_checkout(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _cache(pod, max_bytes=15 * KB, low_bytes=10 * KB)
    a = _golden(pod, "a")
    _later(pod)
    _publish_temporal(pod, "repo", 10 * KB)
    events: List[bool] = []
    real_checkout = pod.artifacts.checkout

    def checkout(*args: Any, **kwargs: Any) -> Any:
        events.append(_served(a))
        return real_checkout(*args, **kwargs)

    monkeypatch.setattr(pod.artifacts, "checkout", checkout)
    root = _temporal_root(pod, "repo")
    tis.ensure_temporal_root(root, _temporal_doc(pod, "repo"), "repo", store=pod.store)
    assert events == [False] and cache.misses == 1


def test_every_fast_path_asks_the_cache(pod: SimpleNamespace) -> None:
    cache = _cache(pod)
    _publish(pod, "activated.alice", KB)
    target = pod.activated / "alice" / "repo"
    _serve_activated(pod, target, "activated.alice")
    _serve_activated(pod, target, "activated.alice")
    _publish_temporal(pod, "repo", KB)
    root = _temporal_root(pod, "repo")
    doc = _temporal_doc(pod, "repo")
    tis.ensure_temporal_root(root, doc, "repo", store=pod.store)
    tis.ensure_temporal_root(root, doc, "repo", store=pod.store)
    assert cache.misses == 2 and cache.hits == 2


def test_a_temporal_reader_racing_the_eviction_checks_out_again(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod)
    _publish_temporal(pod, "repo", KB)
    root = _temporal_root(pod, "repo")
    doc = _temporal_doc(pod, "repo")
    tis.ensure_temporal_root(root, doc, "repo", store=pod.store)
    with cache._lock:
        cache._evicting.add(str(root))
    shutil.rmtree(Path(os.path.realpath(root)))  # what the evictor is doing
    root.unlink()
    tis.ensure_temporal_root(root, doc, "repo", store=pod.store)
    assert (root / "shard" / "data.bin").is_file()


def test_an_activated_reader_racing_the_eviction_checks_out_again(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod)
    _publish(pod, "activated.alice", KB)
    target = pod.activated / "alice" / "repo"
    _serve_activated(pod, target, "activated.alice")
    with cache._lock:
        cache._evicting.add(str(target))
    # the evictor renamed the index away after the reader's fast check
    shutil.rmtree(target / INDEX_DIRNAME)
    (target / INDEX_DIRNAME / "index").mkdir(parents=True)  # stale view
    (target / im.CHECKED_OUT_MARKER).unlink()
    _serve_activated(pod, target, "activated.alice")
    assert (target / INDEX_DIRNAME / "index" / "hnsw.bin").is_file()


def test_health_and_cache_stats_routes_carry_the_cache_only_when_one_runs(
    pod: SimpleNamespace,
) -> None:
    from unittest.mock import Mock

    from fastapi.testclient import TestClient

    from code_indexer.server.app import app
    from code_indexer.server.auth.dependencies import get_current_user

    app.dependency_overrides[get_current_user] = lambda: Mock(username="admin")
    try:
        client = TestClient(app, raise_server_exceptions=False)
        for path in ("/health", "/cache/stats"):
            assert "served_index_cache" not in client.get(path).json()
        cache = _cache(pod)
        _publish(pod, "a", KB)
        _serve_golden(pod, _snapshot(pod, "a"), "a")
        cache.run_pass()
        for path in ("/health", "/cache/stats"):
            served = client.get(path).json()["served_index_cache"]
            assert served["entries"] == 1 and served["misses"] == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("kind", ["golden", "activated", "temporal"])
def test_a_reader_that_waited_for_anothers_checkout_counts_as_a_hit(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    cache = _cache(pod)
    if kind == "golden":
        _publish(pod, "a", KB)
        target = _snapshot(pod, "a")

        def serve() -> None:
            _serve_golden(pod, target, "a")

    elif kind == "activated":
        _publish(pod, "activated.alice", KB)
        target = pod.activated / "alice" / "repo"

        def serve() -> None:
            _serve_activated(pod, target, "activated.alice")

    else:
        _publish_temporal(pod, "repo", KB)
        target = _temporal_root(pod, "repo")
        doc = _temporal_doc(pod, "repo")

        def serve() -> None:
            tis.ensure_temporal_root(target, doc, "repo", store=pod.store)

    in_checkout, release = threading.Event(), threading.Event()
    real_checkout = pod.artifacts.checkout

    def slow_checkout(*args: Any, **kwargs: Any) -> Any:
        in_checkout.set()
        release.wait(5)
        return real_checkout(*args, **kwargs)

    monkeypatch.setattr(pod.artifacts, "checkout", slow_checkout)
    first = threading.Thread(target=serve)
    first.start()
    assert in_checkout.wait(5)
    second = threading.Thread(target=serve)
    second.start()
    second.join(0.2)
    release.set()
    first.join(5)
    second.join(5)
    assert cache.misses == 1 and cache.hits == 1
    assert len(_checkouts(pod)) == 1  # one download for both readers


def test_a_size_is_measured_again_when_parts_or_versions_change_in_one_tick(
    pod: SimpleNamespace,
) -> None:
    """Coarse mtimes (an overlay filesystem): a member added, or a version
    swapped in, within one mtime tick still invalidates the cached size."""
    cache = _cache(pod)
    root = pod.activated / "alice" / "combo"
    _publish(pod, "activated.combo", KB, index=False)
    _serve_activated(pod, root, "activated.combo", require_index=False)
    os.utime(root / im.CHECKED_OUT_MARKER, (T0, T0))
    cache.run_pass()
    alone = cache.cache_bytes()
    _publish(pod, "activated.combo/m1", 10 * KB)
    _serve_activated(pod, root / "m1", "activated.combo/m1")
    os.utime(root / "m1" / im.CHECKED_OUT_MARKER, (T0, T0))
    cache.run_pass()
    assert cache.cache_bytes() > alone + 9 * KB
    with_member = cache.cache_bytes()
    shutil.rmtree(root / "m1" / INDEX_DIRNAME)
    _payload(root / "m1", 20 * KB)
    (root / "m1" / im.CHECKED_OUT_MARKER).write_text("v-other")
    os.utime(root / "m1" / im.CHECKED_OUT_MARKER, (T0, T0))
    cache.run_pass()
    assert cache.cache_bytes() > with_member + 9 * KB


# ── review fixes: a reader losing to a finished eviction; a failing victim ──


def test_a_reader_that_lost_the_race_to_a_finished_eviction_checks_out_again(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fast check passes, then an eviction runs to completion (its
    ``_evicting`` mark already cleared) before the reader's ``note_hit``: the
    hit must still be refused, so the reader checks the index out again."""
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)
    real = im._serves_recorded
    evicted: List[List[str]] = []

    def check_then_lose_the_race(*args: Any) -> bool:
        seen = real(*args)
        if not evicted:
            evicted.append(cache.run_pass().evicted)
            _limit(cache, 0)  # the reader's own checkout is admitted
        return seen

    monkeypatch.setattr(im, "_serves_recorded", check_then_lose_the_race)
    hits = cache.hits
    _serve_golden(pod, a, "a")
    assert evicted == [[str(a)]]
    assert _served(a) and (a / im.CHECKED_OUT_MARKER).is_file()
    assert len(_checkouts(pod)) == 2
    assert cache.hits == hits  # the stale fast path was not counted as a hit


def test_a_hit_on_an_evicted_target_is_refused_after_the_eviction(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod)
    a = _golden(pod, "a")
    _publish_temporal(pod, "repo", KB)
    root = _temporal_root(pod, "repo")
    tis.ensure_temporal_root(root, _temporal_doc(pod, "repo"), "repo", store=pod.store)
    _limit(cache, KB // 2)
    os.utime(sic.temporal_marker(root), (pod.clock.now, pod.clock.now))
    assert cache.note_hit(a) and cache.note_hit(root)
    _later(pod)
    assert sorted(cache.run_pass().evicted) == sorted([str(a), str(root)])
    hits = cache.hits
    assert not cache.note_hit(a) and not cache.note_hit(root)
    assert cache.hits == hits


def _two_cold(pod: SimpleNamespace) -> Any:
    cache = _cache(pod, max_bytes=15 * KB, low_bytes=10 * KB)
    a = _golden(pod, "a")
    _later(pod, 1)
    b = _golden(pod, "b")
    _later(pod)
    return cache, a, b


def test_a_victim_that_cannot_be_retired_is_skipped_and_the_next_is_evicted(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, a, b = _two_cold(pod)
    real_rename = os.rename

    def rename(src: Any, dst: Any) -> None:
        if Path(src) == a / INDEX_DIRNAME:
            raise PermissionError(13, "Permission denied", str(src))
        real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    report = cache.run_pass()
    assert report.skipped[str(a)] == sic.SKIP_ERROR
    assert report.evicted == [str(b)]
    assert _served(a) and not _served(b)
    assert cache.stats()["skips"][sic.SKIP_ERROR] == 1
    assert str(a) not in cache._evicting


def test_a_failing_victim_does_not_fail_an_unrelated_admission(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, a, b = _two_cold(pod)
    _publish(pod, "c", 10 * KB)
    real_rename = os.rename

    def rename(src: Any, dst: Any) -> None:
        if Path(src) == a / INDEX_DIRNAME:
            raise OSError(5, "Input/output error", str(src))
        real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    c = _snapshot(pod, "c")
    _serve_golden(pod, c, "c")  # must not raise for a's sake
    assert _served(c) and _served(a) and not _served(b)


def test_a_half_retired_victim_frees_its_bytes_and_is_finished_next_pass(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rename succeeds and the marker's unlink raises: what was renamed
    aside is deleted, the target is not served (the marker left behind
    vouches for nothing), and its next serve checks it out again."""
    cache, a, b = _two_cold(pod)
    real_unlink = Path.unlink

    def unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == a / im.CHECKED_OUT_MARKER:
            raise PermissionError(13, "Permission denied", str(self))
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    report = cache.run_pass()
    assert report.skipped[str(a)] == sic.SKIP_ERROR
    assert not _served(a)
    assert not any(p.name.startswith(sic.EVICTED_PREFIX) for p in a.iterdir())
    assert not im.is_served_here(pod.store, a)  # the stale marker vouches for nothing
    assert report.evicted == []  # a's bytes are gone: b need not go
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert _served(b)
    _limit(cache, 0)
    _serve_golden(pod, a, "a")
    assert _served(a) and len(_checkouts(pod)) == 3


def test_a_refresh_base_used_by_a_build_is_not_the_first_victim(
    pod: SimpleNamespace,
) -> None:
    """The publisher's ``golden-repos/<repo>`` is never served to a query; a
    refresh using it as its incremental base records it as used, so a colder
    served snapshot goes first -- and the use is not counted as a hit."""
    cache = _two_over(pod)
    base = pod.golden / "a"
    _publish(pod, "a", 10 * KB)
    with _unlimited(pod):
        _serve_golden(pod, base, "a")
    _age_marker(base, pod.clock.now)
    _later(pod, 1)
    _golden(pod, "b")
    _later(pod)
    cache.note_used(base)  # the refresh's build
    assert cache.hits == 0
    assert (base / sic.LAST_SERVED_STAMP).is_file()
    _later(pod)
    assert str(base) not in cache.run_pass().evicted  # EVO-64616: never
    assert _served(base)


def test_a_query_with_a_plain_ref_is_kept_by_the_access_its_serve_recorded(
    pod: SimpleNamespace,
) -> None:
    """The MCP search path takes its ref with ``increment_ref`` (not the
    quiescing-aware one) AFTER resolving its alias, which serves the target
    (``note_hit``). That serve's access makes the checkout recent, so an
    eviction between the serve and the ref skips it."""
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)  # both must go
    a = _golden(pod, "a")
    _later(pod, 1)
    b = _golden(pod, "b")
    _later(pod)
    _serve_golden(pod, a, "a")  # the query resolves its alias ...
    report = cache.run_pass()  # ... an eviction lands before its ref ...
    pod.tracker.increment_ref(str(a))  # ... then the plain ref
    assert report.skipped[str(a)] == sic.SKIP_RECENT
    assert report.evicted == [str(b)] and _served(a)
    pod.tracker.decrement_ref(str(a))


# ── more than one server process per pod (uvicorn --workers 2) ──────────────


def _in_child(work: Callable[[], Callable[[], None]]) -> Callable[[], None]:
    """Run ``work`` in a forked process that then waits; returns ``finish``,
    which tells it to undo and exit. ``work`` returns an undo callable."""
    ready_r, ready_w = os.pipe()
    done_r, done_w = os.pipe()
    pid = os.fork()
    if pid == 0:  # the child: another server process on the same pod
        try:
            undo = work()
            os.write(ready_w, b"x")
            os.read(done_r, 1)
            undo()
            os.write(ready_w, b"y")
        finally:
            os._exit(0)
    assert os.read(ready_r, 1) == b"x"

    def finish() -> None:
        os.write(done_w, b"x")
        assert os.read(ready_r, 1) == b"y"
        os.waitpid(pid, 0)
        for fd in (ready_r, ready_w, done_r, done_w):
            os.close(fd)

    return finish


def test_a_query_in_another_process_keeps_the_checkout(pod: SimpleNamespace) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)

    def query() -> Callable[[], None]:
        other = QueryTracker()  # the other process's own tracker
        other.increment_ref(str(a))
        return lambda: other.decrement_ref(str(a))

    finish = _in_child(query)
    try:
        report = cache.run_pass()
        assert report.skipped[str(a)] == sic.SKIP_IN_USE and _served(a)
    finally:
        finish()
    assert cache.run_pass().evicted == [str(a)]


def test_an_eviction_in_another_process_turns_a_hit_away(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod)
    a = _golden(pod, "a")

    def evicting() -> Callable[[], None]:
        fd = os.open(str(sic.readers_lock_path(a)), os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return lambda: os.close(fd)

    finish = _in_child(evicting)
    try:
        assert not cache.note_hit(a)
    finally:
        finish()
    assert cache.note_hit(a)


def test_a_tracked_query_holds_the_reader_lock_until_it_ends(
    pod: SimpleNamespace,
) -> None:
    _cache(pod)
    a = _golden(pod, "a")
    tracker = QueryTracker()
    with tracker.track_query(str(a)):
        with tracker.track_query(str(a)):
            with sic._readers_free([a]) as free:
                assert not free
        with sic._readers_free([a]) as free:
            assert not free  # one query still in flight
    with sic._readers_free([a]) as free:
        assert free
    assert tracker.try_increment_ref_if_not_quiescing(str(a))
    with sic._readers_free([a]) as free:
        assert not free
    tracker.decrement_ref(str(a))
    with sic._readers_free([a]) as free:
        assert free


def test_without_a_cache_the_tracker_takes_no_lock_and_writes_nothing(
    tmp_path: Path,
) -> None:
    sic.install_served_index_cache(None)
    target = tmp_path / "golden" / "repo"
    target.mkdir(parents=True)
    before = sorted(tmp_path.rglob("*"))
    tracker = QueryTracker()
    with tracker.track_query(str(target)):
        assert sorted(tmp_path.rglob("*")) == before
    assert sorted(tmp_path.rglob("*")) == before


def test_a_failing_reader_lock_never_fails_the_query(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _cache(pod)
    a = _golden(pod, "a")

    def broken(path: str) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(cache, "hold_reader", broken)
    sic.install_served_index_cache(cache)
    tracker = QueryTracker()
    with tracker.track_query(str(a)):
        assert tracker.get_ref_count(str(a)) == 1
    assert tracker.get_ref_count(str(a)) == 0


def test_a_path_with_no_parent_takes_no_reader_lock(tmp_path: Path) -> None:
    cache = sic.ServedIndexCache(activated_repos_dir=None, golden_repos_dir=None)
    ghost = tmp_path / "missing" / "target"
    cache.hold_reader(str(ghost))
    cache.release_reader(str(ghost))
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("composite", [False, True])
def test_an_activation_is_pinned_from_seed_to_publish(
    tmp_path: Path, composite: bool
) -> None:
    """A re-activation onto a path with a stale checkout marker: between
    seeding the index and publishing it, the copy is never evictable."""
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    target = tmp_path / "alice" / "copy"
    seen: List[Any] = []

    def run(*args: Any) -> Dict[str, Any]:
        seen.append((sic.is_pinned(target), args))
        return {}

    fake = SimpleNamespace(
        _safe_user_scoped_path=lambda user, alias: str(tmp_path / user / alias),
        _default_composite_alias=lambda aliases: "copy",
        _run_activate_repository=run,
        _run_activate_composite_repository=run,
    )
    fake._activation_pin = lambda user, alias: ActivatedRepoManager._activation_pin(
        cast(Any, fake), user, alias
    )
    if composite:
        ActivatedRepoManager._do_activate_composite_repository(
            cast(Any, fake), "alice", ["g1", "g2"], None, None
        )
        assert seen == [(True, ("alice", ["g1", "g2"], None, None))]
    else:
        ActivatedRepoManager._do_activate_repository(
            cast(Any, fake), "alice", "golden", "main", "copy", None, None
        )
        assert seen == [(True, ("alice", "golden", "main", "copy", None, None))]
    assert not sic.is_pinned(target)


def test_an_unsafe_activation_path_pins_nothing(tmp_path: Path) -> None:
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoError,
        ActivatedRepoManager,
    )

    def unsafe(user: str, alias: str) -> str:
        raise ActivatedRepoError("escapes")

    fake = SimpleNamespace(_safe_user_scoped_path=unsafe)
    with ActivatedRepoManager._activation_pin(cast(Any, fake), "alice", "../x"):
        assert sic.pin_count() == 0


# ── read scopes: a read holds what it served for its whole duration ─────────


def test_a_read_scope_keeps_what_it_served_until_it_ends(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)
    with sic.served_index_reads():
        _serve_golden(pod, a, "a")  # a hit: held from here on
        _later(pod)  # however long the read takes
        assert cache.run_pass().skipped[str(a)] == sic.SKIP_IN_USE
        assert _served(a)
    assert cache.run_pass().evicted == [str(a)]


def test_a_checkout_made_inside_a_read_scope_is_held_too(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod)
    _publish(pod, "a", 10 * KB)
    a = _snapshot(pod, "a")
    with sic.served_index_reads():
        _serve_golden(pod, a, "a")  # a miss: checked out, then held
        _limit(cache, 5 * KB)
        _later(pod)
        assert cache.run_pass().skipped[str(a)] == sic.SKIP_IN_USE
    assert cache.run_pass().evicted == [str(a)]


def test_read_scopes_nest_and_the_outer_one_releases(pod: SimpleNamespace) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    with sic.served_index_reads():
        with sic.served_index_reads():
            _serve_golden(pod, a, "a")
        _later(pod)  # the inner read ended; the outer one still holds it
        assert cache.run_pass().skipped[str(a)] == sic.SKIP_IN_USE
    with sic._readers_free([a]) as free:
        assert free


def test_a_read_holds_one_lock_per_checkout_however_often_it_serves_it(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod)
    _publish(pod, "a", KB)
    a = _snapshot(pod, "a")
    with sic.served_index_reads():
        _serve_golden(pod, a, "a")  # a checkout
        _serve_golden(pod, a, "a")  # then hits
        _serve_golden(pod, a, "a")
        scope = sic._read_scope.get()
        assert scope is not None and list(scope._fds) == [str(a)]
    assert cache.misses == 1 and cache.hits == 2


def test_a_thread_that_inherits_the_context_holds_for_the_read(
    pod: SimpleNamespace,
) -> None:
    import contextvars

    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)
    with sic.served_index_reads():
        ctx = contextvars.copy_context()
        worker = threading.Thread(target=ctx.run, args=(_serve_golden, pod, a, "a"))
        worker.start()
        worker.join(5)
        _later(pod)
        assert cache.run_pass().skipped[str(a)] == sic.SKIP_IN_USE
    assert cache.run_pass().evicted == [str(a)]


def test_a_serve_outside_any_read_scope_holds_nothing(pod: SimpleNamespace) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _serve_golden(pod, a, "a")
    with sic._readers_free([a]) as free:
        assert free
    _later(pod)
    assert cache.run_pass().evicted == [str(a)]


def test_a_read_scope_in_another_process_keeps_the_checkout(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)

    def read() -> Callable[[], None]:
        scope = sic.served_index_reads()
        scope.__enter__()
        assert cache.note_hit(a)

        def undo() -> None:
            scope.__exit__(None, None, None)

        return undo

    finish = _in_child(read)
    try:
        assert cache.run_pass().skipped[str(a)] == sic.SKIP_IN_USE
    finally:
        finish()
    assert cache.run_pass().evicted == [str(a)]


def test_the_decorator_scopes_sync_and_async_reads_and_keeps_the_signature(
    pod: SimpleNamespace,
) -> None:
    import asyncio
    import inspect

    _cache(pod)
    a = _golden(pod, "a")

    @sic.reads_served_indexes
    def read(alias: str, *, limit: int = 3) -> bool:
        _serve_golden(pod, a, alias)
        with sic._readers_free([a]) as free:
            return free

    @sic.reads_served_indexes
    async def aread(alias: str) -> bool:
        _serve_golden(pod, a, alias)
        with sic._readers_free([a]) as free:
            return free

    assert read("a") is False and asyncio.run(aread("a")) is False
    assert list(inspect.signature(read).parameters) == ["alias", "limit"]
    with sic._readers_free([a]) as free:
        assert free


def test_without_a_cache_a_read_scope_holds_and_writes_nothing(
    tmp_path: Path,
) -> None:
    sic.install_served_index_cache(None)
    before = sorted(tmp_path.rglob("*"))
    with sic.served_index_reads():
        im.ensure_published_index(tmp_path / "x", None, store=LocalIndexStore())
    assert sorted(tmp_path.rglob("*")) == before


# ── pins hold the readers lock; quiescing marks; the loop interval ──────────


def _exclusive_free(target: Path) -> bool:
    """What an eviction in another process sees: may it take the target's
    readers lock EXCLUSIVE right now?"""
    with sic.readers_free([target]) as free:
        return free


def test_a_pin_holds_the_readers_lock_shared_for_its_whole_run(
    pod: SimpleNamespace,
) -> None:
    _cache(pod)
    a = _golden(pod, "a")
    assert _exclusive_free(a)
    with sic.pinned_served_index(a):
        assert not _exclusive_free(a)
        with sic.pinned_served_index(a):  # re-entrant: one more shared hold
            assert not _exclusive_free(a)
        assert not _exclusive_free(a)
    assert _exclusive_free(a)


def test_a_pin_in_another_process_blocks_eviction(pod: SimpleNamespace) -> None:
    """The in-memory pin is invisible to another process; the readers lock
    is not. The pin is taken in a forked child, the pass runs here."""
    import multiprocessing

    cache = _two_over(pod)
    a = _golden(pod, "a")
    _later(pod, 1)
    b = _golden(pod, "b")
    _later(pod)
    ctx = multiprocessing.get_context("fork")
    pinned, done = ctx.Event(), ctx.Event()

    def child() -> None:
        with sic.pinned_served_index(a):
            pinned.set()
            done.wait(10)

    proc = ctx.Process(target=child)
    proc.start()
    try:
        assert pinned.wait(10)
        assert not sic.is_pinned(a)  # nothing in THIS process's memory
        report = cache.run_pass()
    finally:
        done.set()
        proc.join(10)
    assert report.skipped[str(a)] == sic.SKIP_IN_USE and _served(a)
    assert report.evicted == [str(b)]


def test_a_pin_waits_out_an_eviction_already_running(pod: SimpleNamespace) -> None:
    """A pin taken after the eviction's last pin check and before its retire
    waits until the retire is done -- it can never overlap it."""
    cache = _two_over(pod)
    a = _golden(pod, "a")
    _later(pod, 1)
    _golden(pod, "b")
    _later(pod)
    entered = threading.Event()
    seen: List[bool] = []
    real_retire = sic.ServedIndexCache._retire

    def pin_then_body() -> None:
        with sic.pinned_served_index(a):
            entered.set()

    def retire(entry: sic.ServedCheckout) -> List[Optional[Path]]:
        if entry.key == str(a):
            pinner = threading.Thread(target=pin_then_body)
            pinner.start()
            seen.append(entered.wait(0.3))  # must still be waiting
            retired = real_retire(entry)
            pod.pinner = pinner
            return retired
        return real_retire(entry)

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            _patched(sic.ServedIndexCache, "_retire", staticmethod(retire))
        )
        report = cache.run_pass()
    pod.pinner.join(5)
    assert seen == [False], "the pin entered while the retire was running"
    assert entered.is_set() and str(a) in report.evicted


@contextlib.contextmanager
def _patched(owner: Any, name: str, value: Any) -> Iterator[None]:
    original = owner.__dict__[name]
    setattr(owner, name, value)
    try:
        yield
    finally:
        setattr(owner, name, original)


def test_an_eviction_skips_a_key_another_purge_is_quiescing(
    pod: SimpleNamespace,
) -> None:
    cache = _two_over(pod)
    a = _golden(pod, "a")
    _later(pod, 1)
    b = _golden(pod, "b")
    _later(pod)
    pod.tracker.mark_quiescing(str(a))  # a deactivation is purging it
    report = cache.run_pass()
    assert report.skipped[str(a)] == sic.SKIP_QUIESCING and _served(a)
    assert pod.tracker.is_quiescing(str(a)), "the other purge's mark was cleared"
    assert report.evicted == [str(b)]
    assert not pod.tracker.is_quiescing(str(b)), "its own mark was left behind"


def test_try_mark_quiescing_is_test_and_set() -> None:
    tracker = QueryTracker()
    assert tracker.try_mark_quiescing("/x") is True
    assert tracker.try_mark_quiescing("/x") is False
    assert tracker.is_quiescing("/x")
    tracker.clear_quiescing("/x")
    assert tracker.try_mark_quiescing("/x") is True


@pytest.mark.parametrize("interval", [0, -1, 0.0])
def test_a_non_positive_interval_is_refused(
    pod: SimpleNamespace, interval: float
) -> None:
    cache = _cache(pod)
    with pytest.raises(ValueError, match="positive"):
        cache.start(interval)
    assert cache._thread is None


@pytest.mark.parametrize("interval", [0, -5])
def test_startup_refuses_a_non_positive_cache_interval(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, interval: int
) -> None:
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: pod.store)
    app = SimpleNamespace(
        state=SimpleNamespace(
            activated_repo_manager=SimpleNamespace(activated_repos_dir=pod.activated),
            golden_repos_dir=str(pod.golden),
            query_tracker=pod.tracker,
        )
    )
    config = ServerResourceConfig(served_index_cache_interval_seconds=interval)
    with pytest.raises(ValueError, match="served_index_cache_interval_seconds"):
        index_store_wiring.start_served_index_cache(app, config)
    assert sic.active_served_index_cache() is None


# ── carried reads: work that outlives the read that started it ──────────────


def test_carried_reads_keep_the_checkouts_after_the_read_ends(
    pod: SimpleNamespace,
) -> None:
    _cache(pod)
    a = _golden(pod, "a")
    started, release = threading.Event(), threading.Event()

    def work() -> str:
        started.set()
        release.wait(5)
        return "done"

    with sic.served_index_reads():
        _serve_golden(pod, a, "a")
        assert not _exclusive_free(a)
        carried = sic.carry_served_index_reads(work)
        worker = threading.Thread(target=carried)
        worker.start()
        assert started.wait(5)
    assert not _exclusive_free(a), "the read ended but the carried work runs on"
    release.set()
    worker.join(5)
    assert _exclusive_free(a), "the carried copy was not released"


def test_an_unused_carry_is_released(pod: SimpleNamespace) -> None:
    _cache(pod)
    a = _golden(pod, "a")
    with sic.served_index_reads():
        _serve_golden(pod, a, "a")
        carried = sic.carry_served_index_reads(lambda: None)
    assert not _exclusive_free(a)
    carried.release()
    carried.release()  # idempotent
    assert _exclusive_free(a)


def test_carried_work_holds_what_it_serves_itself(pod: SimpleNamespace) -> None:
    _cache(pod)
    a = _golden(pod, "a")
    b = _golden(pod, "b")
    observed: List[bool] = []

    def work() -> None:
        _serve_golden(pod, b, "b")
        observed.append(_exclusive_free(b))

    with sic.served_index_reads():
        _serve_golden(pod, a, "a")
        carried = sic.carry_served_index_reads(work)
    carried()
    assert observed == [False] and _exclusive_free(a) and _exclusive_free(b)


def test_without_a_read_scope_carry_runs_the_function_unchanged() -> None:
    carried = sic.carry_served_index_reads(lambda x: x + 1)
    assert carried(1) == 2
    carried.release()


def test_a_pin_under_the_local_store_touches_no_file(tmp_path: Path) -> None:
    sic.install_served_index_cache(None)
    target = tmp_path / "repo"
    target.mkdir()
    with sic.pinned_served_index(target, "/x"):
        assert sic.is_pinned(target)
    assert not sic.readers_lock_path(target).exists()
