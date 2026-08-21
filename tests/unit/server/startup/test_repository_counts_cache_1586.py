"""Story #1586 code-review Finding 1 (BLOCKING): the cidx.repos.total/
cidx.repos.indexed observable-gauge callback must never do an unpaced,
O(fleet) blocking filesystem scan on the OTEL SDK's exporter thread.

Background: _build_repository_counts_callback()'s returned callback used to
call golden_repo_manager.list_golden_repos() + get_golden_repo() +
_index_exists() PER REPO synchronously on every single invocation.
JobMetrics registers TWO separate observable-gauge callbacks
(_observe_total_repos, _observe_indexed_repos) that EACH call this same
callback, so the walk ran twice per collection cycle. At production scale
(~900 repos, per this project's documented "design for 900" rule) this
becomes tens of thousands of filesystem/NFS metadata ops per 60s export
cycle, and OTEL's SynchronousMeasurementConsumer.collect() enforces
export_timeout_millis (default 30s) checked only BETWEEN callbacks -- never
during one -- so a slow or wedged ("hard" NFSv3 can block FOREVER) pass can
discard an entire cycle's metrics or hang MeterProvider.shutdown().

Fix: _RepositoryCountsCache makes the callback itself O(1) -- it always
returns the last-computed value immediately and refreshes that value on a
background daemon thread at most once per refresh_interval_seconds,
single-flighted. A stalled refresh never blocks a caller.

These tests prove the fix at SYNTHESIZED FLEET CARDINALITY (900 repos) per
this project's explicit "prove it at 900, not at 20" rule -- a fake,
call-counting golden_repo_manager double stands in for the real
GoldenRepoManager (an external collaborator, not the code under test) so
900-repo behavior can be proven without 900 real on-disk repos.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

from code_indexer.server.startup.lifespan import _build_repository_counts_callback

# Synthesized fleet cardinality this project's CLAUDE.md requires proving
# behavior at ("design for 900, not the 30-repo dev server").
FLEET_REPO_COUNT = 900
# How many times the warm-cache test calls callback() in a row to prove
# repeated calls stay O(1) rather than accumulating cost.
CALLBACK_REPEAT_COUNT = 50
# Upper bound on total elapsed time for the O(1)/never-blocks assertions --
# generous relative to real scan cost (milliseconds) but far below the
# multi-second stall/scan durations this fix must never expose to a caller.
MAX_CALLBACK_ELAPSED_SECONDS = 0.25
# Simulated wedged-NFS stall duration for the background refresh.
STALLED_SCAN_DELAY_SECONDS = 2.0
# Bounded wait for the background priming refresh to finish before the
# warm-cache assertions run.
CACHE_WARMUP_TIMEOUT_SECONDS = 10.0
# Bounded wait for the stalled background refresh to have actually started
# scanning before timing the non-blocking callback() call.
STALL_STARTED_WAIT_TIMEOUT_SECONDS = 5.0
# A refresh interval long enough that CALLBACK_REPEAT_COUNT calls all stay
# within one refresh window (no unwanted re-scan mid-assertion).
LONG_REFRESH_INTERVAL_SECONDS = 900.0
# A refresh interval short enough that the very first callback() call is
# guaranteed to trigger the (stalled) background refresh immediately.
IMMEDIATE_REFRESH_INTERVAL_SECONDS = 0.01
# Expected repo counts before any background refresh has completed.
EMPTY_REPOSITORY_COUNT = 0
# Exactly one background priming scan should ever run per warm-cache test.
EXPECTED_PRIMING_SCAN_CALLS = 1


class _FakeGoldenRepoManagerAtScale:
    """Fake, call-counting golden_repo_manager double simulating a fleet of
    `repo_count` repos, each already indexed. Not a mock of the code under
    test (_RepositoryCountsCache lives in lifespan.py) -- this fakes ONLY
    the external collaborator (GoldenRepoManager), the minimum 3-method
    interface _build_repository_counts_callback()'s compute step calls.

    `list_calls` is Lock-protected: list_golden_repos() runs on a
    background refresh thread while the test thread reads the counter.
    `list_started` lets a test wait deterministically for a stall to be
    underway instead of guessing with a sleep.
    """

    def __init__(self, repo_count: int, per_call_delay: float = 0.0) -> None:
        self.repo_count = repo_count
        self.per_call_delay = per_call_delay
        self._lock = threading.Lock()
        self.list_calls = 0
        self.list_started = threading.Event()

    def list_golden_repos(self) -> List[Dict[str, Any]]:
        with self._lock:
            self.list_calls += 1
        self.list_started.set()
        if self.per_call_delay:
            time.sleep(self.per_call_delay)
        return [{"alias": f"repo-{i}"} for i in range(self.repo_count)]

    def get_golden_repo(self, alias: str) -> Dict[str, Any]:
        return {"alias": alias}

    def _index_exists(self, golden_repo: Dict[str, Any], kind: str) -> bool:
        return True


class TestRepositoryCountsCallbackFleetScaleCaching:
    def test_callback_completes_near_instantly_at_900_repos_after_warm_cache(self):
        fake_manager = _FakeGoldenRepoManagerAtScale(repo_count=FLEET_REPO_COUNT)
        callback = _build_repository_counts_callback(
            fake_manager, refresh_interval_seconds=LONG_REFRESH_INTERVAL_SECONDS
        )
        warmed_up = callback.cache.wait_for_idle(timeout=CACHE_WARMUP_TIMEOUT_SECONDS)
        assert warmed_up is True, (
            "background priming refresh did not complete within the "
            "warm-up timeout -- cannot validate warm-cache behavior"
        )
        with fake_manager._lock:
            assert fake_manager.list_calls == EXPECTED_PRIMING_SCAN_CALLS, (
                "exactly one background priming scan is expected before "
                "the first callback() call"
            )

        start = time.monotonic()
        counts = None
        for _ in range(CALLBACK_REPEAT_COUNT):
            counts = callback()
        elapsed = time.monotonic() - start

        assert counts == {"total": FLEET_REPO_COUNT, "indexed": FLEET_REPO_COUNT}
        assert elapsed < MAX_CALLBACK_ELAPSED_SECONDS, (
            f"{CALLBACK_REPEAT_COUNT} callback() calls at "
            f"{FLEET_REPO_COUNT}-repo fleet cardinality took {elapsed:.4f}s "
            f"-- the callback must be O(1) per call, never re-scanning the "
            f"fleet on the OTEL exporter thread"
        )
        with fake_manager._lock:
            assert fake_manager.list_calls == EXPECTED_PRIMING_SCAN_CALLS, (
                "within the refresh interval, repeated callback() calls "
                "must not trigger additional list_golden_repos() scans"
            )

    def test_stalled_refresh_never_blocks_callback(self):
        """A stuck refresh (simulating a wedged 'hard' NFS mount) must
        never block callback() -- it must return the last-good (here:
        zeroed default, since no refresh has completed yet) value
        immediately instead of waiting on the in-flight scan.
        """
        fake_manager = _FakeGoldenRepoManagerAtScale(
            repo_count=FLEET_REPO_COUNT, per_call_delay=STALLED_SCAN_DELAY_SECONDS
        )
        callback = _build_repository_counts_callback(
            fake_manager, refresh_interval_seconds=IMMEDIATE_REFRESH_INTERVAL_SECONDS
        )
        # Deterministically wait for the background priming refresh to
        # actually be mid-stall before measuring callback() -- never guess
        # with a sleep.
        started = fake_manager.list_started.wait(
            timeout=STALL_STARTED_WAIT_TIMEOUT_SECONDS
        )
        assert started is True, (
            "background priming refresh never started scanning -- cannot "
            "validate stalled-refresh behavior"
        )

        start = time.monotonic()
        counts = callback()
        elapsed = time.monotonic() - start

        assert elapsed < MAX_CALLBACK_ELAPSED_SECONDS, (
            f"callback() blocked for {elapsed:.4f}s waiting on an "
            f"in-flight refresh -- it must always return immediately"
        )
        assert counts == {
            "total": EMPTY_REPOSITORY_COUNT,
            "indexed": EMPTY_REPOSITORY_COUNT,
        }
