"""The ordinary job pool is a NODE budget, so it divides by the worker count.

``max_concurrent_background_jobs`` reads as "how many background jobs this
server runs at once". It was applied per uvicorn worker PROCESS, so the default
of 5 meant 10 threads on a 2-worker pod and 60 across the tools-* StatefulSet,
all pulling on one Postgres and one NFS mount.

Dividing is easy to get subtly wrong in one specific way: the pool is sized in
``__init__`` and drained in ``shutdown()``, and if those two disagree about how
many workers exist, ``shutdown()`` under-sends sentinels and the surplus threads
block on ``queue.get()`` forever. That symmetry is what most of these tests are
about.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from code_indexer.server.repositories import background_jobs as bgj
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.utils.config_manager import BackgroundJobsConfig


def _manager(tmp_path: Path, workers: int, monkeypatch, **cfg) -> BackgroundJobManager:
    monkeypatch.setattr(bgj, "detect_worker_count", lambda: workers)
    return BackgroundJobManager(
        storage_path=str(tmp_path / "jobs.json"),
        background_jobs_config=BackgroundJobsConfig(**cfg),
    )


def test_pool_size_is_the_config_value_divided_by_workers(tmp_path, monkeypatch):
    m = _manager(tmp_path, 2, monkeypatch, max_concurrent_background_jobs=5)
    try:
        assert m._pool_size == 2
    finally:
        m.shutdown()


def test_a_single_worker_node_is_unchanged(tmp_path, monkeypatch):
    """The VM/solo deployment must see exactly today's behaviour."""
    m = _manager(tmp_path, 1, monkeypatch, max_concurrent_background_jobs=5)
    try:
        assert m._pool_size == 5
    finally:
        m.shutdown()


def test_pool_never_shrinks_to_zero(tmp_path, monkeypatch):
    """A zero-size pool accepts jobs and runs none -- a hang, not a limit."""
    m = _manager(tmp_path, 12, monkeypatch, max_concurrent_background_jobs=5)
    try:
        assert m._pool_size == 1
    finally:
        m.shutdown()


def test_the_pool_actually_has_that_many_threads(tmp_path, monkeypatch):
    """Pin the number against the real threads, not just the attribute.

    Both pools register under the same ``_pool_`` key prefix, so the count is
    ordinary + temporal -- and the temporal lane is NOT divided (see below),
    which is exactly why its size appears here undivided.
    """
    m = _manager(tmp_path, 2, monkeypatch, max_concurrent_background_jobs=5)
    try:
        pool_threads = [k for k in m._running_jobs if k.startswith("_pool_")]
        assert m._pool_size == 2
        assert len(pool_threads) == m._pool_size + m._temporal_pool_size
    finally:
        m.shutdown()


def test_shutdown_drains_the_pool_it_actually_built(tmp_path, monkeypatch):
    """One sentinel per worker, counted from the SAME number that sized it.

    If shutdown re-derives the count from config it sends 5 sentinels to a
    2-thread pool (harmless) -- but re-derive it from a worker count that has
    since changed, or divide in only one of the two places, and the surplus
    threads block on get() forever and the process never exits.
    """
    m = _manager(tmp_path, 2, monkeypatch, max_concurrent_background_jobs=5)
    # Count the sentinels as they are PUT: the live pool threads consume them
    # immediately, so draining the queue afterwards finds nothing.
    sentinels = []
    real_put = m._pending_job_queue.put

    def _spy(item, *a, **kw):
        if item is None:
            sentinels.append(item)
        return real_put(item, *a, **kw)

    monkeypatch.setattr(m._pending_job_queue, "put", _spy)
    m.shutdown()
    assert len(sentinels) == m._pool_size


def test_shutdown_does_not_re_derive_the_pool_size() -> None:
    """Structural: shutdown must read the stored size, not recompute it.

    Behavioural tests pass either way as long as both sides happen to agree
    today; this is the thing that must not drift tomorrow.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(BackgroundJobManager.shutdown)))
    reads_config = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr == "max_concurrent_background_jobs"
    ]
    assert not reads_config, (
        "shutdown() re-reads max_concurrent_background_jobs instead of the "
        "pool size it was built with -- the two can disagree, and when they do "
        "the surplus pool threads never wake and shutdown hangs."
    )


@pytest.mark.parametrize("workers", [1, 2, 12])
def test_temporal_lane_is_deliberately_not_divided(tmp_path, monkeypatch, workers):
    """The isolation lane keeps its full size per process, on purpose.

    It defaults to 2; halving it on a 2-worker node leaves 1, and the whole
    point of the separate lane is that a saturated ordinary pool cannot starve
    temporal queries. If this ever should divide, it needs its own decision --
    not absorption into a pass about a different budget.

    The first version of this asserted on ``inspect.getsource`` text:
    ``"per_process_budget(\\n" not in src.split("temporal_lane_concurrency")[-1]``.
    Review showed it could not fail. Written the natural multi-line way --
    exactly like the ordinary pool directly above it --

        self._temporal_pool_size = per_process_budget(
            self._background_jobs_config.temporal_lane_concurrency,
            detect_worker_count(),
        )

    the ``per_process_budget(`` opener sits BEFORE the last occurrence of the
    token, so ``split(...)[-1]`` excludes it and the assertion stayed green
    while the lane was halved. A one-line call form passed too. The MR
    description claimed this was pinned; it was not.
    """
    m = _manager(
        tmp_path,
        workers,
        monkeypatch,
        max_concurrent_background_jobs=5,
        temporal_lane_concurrency=2,
    )
    try:
        assert m._temporal_pool_size == 2
    finally:
        m.shutdown()


def test_the_temporal_lane_really_has_its_undivided_threads(tmp_path, monkeypatch):
    """Against the live threads, not the attribute.

    Counted by thread NAME so the ordinary pool cannot stand in for the lane --
    both register under the same ``_pool_`` key in ``_running_jobs``, which is
    why the sibling thread-count test could not tell them apart either.
    """
    import threading

    before = {t.name for t in threading.enumerate()}
    m = _manager(
        tmp_path,
        2,
        monkeypatch,
        max_concurrent_background_jobs=5,
        temporal_lane_concurrency=2,
    )
    try:
        new_threads = [t for t in threading.enumerate() if t.name not in before]
        temporal = [t for t in new_threads if t.name.startswith("bgm-temporal-worker-")]
        ordinary = [t for t in new_threads if t.name.startswith("bgm-worker-")]
        assert len(temporal) == 2, "the temporal lane was divided"
        assert len(ordinary) == 2, "the ordinary pool was not divided"
    finally:
        m.shutdown()


def test_shutdown_drains_the_temporal_lane_it_built(tmp_path, monkeypatch):
    """Same sentinel symmetry as the ordinary pool, for the same reason."""
    m = _manager(
        tmp_path,
        2,
        monkeypatch,
        max_concurrent_background_jobs=5,
        temporal_lane_concurrency=2,
    )
    sentinels = []
    real_put = m._temporal_pending_job_queue.put

    def _spy(item, *a, **kw):
        if item is None:
            sentinels.append(item)
        return real_put(item, *a, **kw)

    monkeypatch.setattr(m._temporal_pending_job_queue, "put", _spy)
    m.shutdown()
    assert len(sentinels) == m._temporal_pool_size == 2


@pytest.mark.parametrize("workers,expected", [(1, 5), (2, 2), (3, 1), (12, 1)])
def test_max_concurrent_jobs_reports_this_process_s_share(
    tmp_path, monkeypatch, workers, expected
):
    """The property feeds queue-depth stats; it must match the real pool.

    Reporting the undivided node number next to a pool half that size is how a
    dashboard says "2/5 running" while the pool is already full.
    """
    m = _manager(tmp_path, workers, monkeypatch, max_concurrent_background_jobs=5)
    try:
        assert m.max_concurrent_jobs == expected == m._pool_size
    finally:
        m.shutdown()
