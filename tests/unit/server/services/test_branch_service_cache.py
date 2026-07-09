"""Regression: list_branches caches per (codebase_dir, include_remote).

The underlying walk reads .commit for every local and remote ref; on repos with
many refs it cost tens of seconds and ran on every get_branches call. A
short-TTL cache makes repeated calls reuse the walk.
"""

from unittest.mock import MagicMock, patch

from code_indexer.server.services import branch_service as bs_mod
from code_indexer.server.services.branch_service import BranchService


def _svc(codebase="/repo"):
    svc = BranchService.__new__(BranchService)
    svc._is_git_repo = True
    svc._closed = False
    svc.repo = None
    gt = MagicMock()
    gt.codebase_dir = codebase
    svc.git_topology_service = gt
    return svc


def test_second_call_hits_cache():
    bs_mod._branches_cache.clear()
    svc = _svc()
    calls = {"n": 0}

    def fake_uncached(self, include_remote=False):
        calls["n"] += 1
        return [MagicMock(name="main")]

    with patch.object(BranchService, "_list_branches_uncached", fake_uncached):
        r1 = svc.list_branches()
        r2 = svc.list_branches()

    assert calls["n"] == 1, f"expected 1 walk (cache hit on 2nd), got {calls['n']}"
    assert r1 is r2


def test_include_remote_is_separate_cache_key():
    bs_mod._branches_cache.clear()
    svc = _svc()
    calls = {"n": 0}

    def fake_uncached(self, include_remote=False):
        calls["n"] += 1
        return []

    with patch.object(BranchService, "_list_branches_uncached", fake_uncached):
        svc.list_branches(include_remote=False)
        svc.list_branches(include_remote=True)  # different key -> separate walk

    assert calls["n"] == 2


def test_non_git_repo_returns_empty_without_walk():
    bs_mod._branches_cache.clear()
    svc = _svc()
    svc._is_git_repo = False
    with patch.object(
        BranchService, "_list_branches_uncached", side_effect=AssertionError
    ):
        assert svc.list_branches() == []


class _FakeClock:
    """Controllable monotonic clock for TTL-anchoring tests."""

    def __init__(self, start: float = 0.0):
        self.value = start

    def monotonic(self) -> float:
        return self.value


def test_slow_walk_still_cached_ttl_anchored_post_walk():
    """Finding 1: TTL must be anchored to POST-walk time, not pre-walk time.

    Simulates a branch walk that takes longer than the TTL (by advancing
    the fake monotonic clock inside the uncached walk). If expiry were
    computed from the PRE-walk timestamp (the bug), the entry would already
    be expired the moment it is stored, and the very next call would
    re-walk. With the fix (expiry anchored post-walk), the next call must
    still hit the cache.
    """
    bs_mod._branches_cache.clear()
    svc = _svc()
    calls = {"n": 0}
    clock = _FakeClock(start=0.0)

    def slow_uncached(self, include_remote=False):
        calls["n"] += 1
        # Simulate a walk that takes longer than the TTL.
        clock.value += bs_mod._BRANCHES_CACHE_TTL_SECONDS + 10.0
        return [MagicMock(name="main")]

    with (
        patch.object(BranchService, "_list_branches_uncached", slow_uncached),
        patch.object(bs_mod.time, "monotonic", clock.monotonic),
    ):
        svc.list_branches()  # first call: slow walk consumes > TTL
        svc.list_branches()  # second call: must still hit cache

    assert calls["n"] == 1, (
        f"expected 1 walk (cache still valid post-walk), got {calls['n']}"
    )


def test_ttl_expiry_triggers_rewalk():
    bs_mod._branches_cache.clear()
    svc = _svc()
    calls = {"n": 0}
    clock = _FakeClock(start=0.0)

    def counting(self, include_remote=False):
        calls["n"] += 1
        return [MagicMock(name="main")]

    with (
        patch.object(BranchService, "_list_branches_uncached", counting),
        patch.object(bs_mod.time, "monotonic", clock.monotonic),
    ):
        svc.list_branches()  # first call: walk + cache
        clock.value += bs_mod._BRANCHES_CACHE_TTL_SECONDS + 1.0  # advance past TTL
        svc.list_branches()  # second call: must re-walk

    assert calls["n"] == 2, f"expected 2 walks (TTL expired), got {calls['n']}"


def test_lru_eviction_evicts_oldest_codebase_dir():
    bs_mod._branches_cache.clear()
    with patch.object(
        BranchService,
        "_list_branches_uncached",
        lambda self, include_remote=False: [],
    ):
        for i in range(bs_mod._BRANCHES_CACHE_MAX + 1):
            _svc(codebase=f"/repo{i}").list_branches()

    assert len(bs_mod._branches_cache) == bs_mod._BRANCHES_CACHE_MAX
    assert ("/repo0", False) not in bs_mod._branches_cache, (
        "oldest entry should be evicted"
    )
    assert (
        f"/repo{bs_mod._BRANCHES_CACHE_MAX}",
        False,
    ) in bs_mod._branches_cache, "newest entry should remain"


def test_invalidate_forces_rewalk_for_codebase_dir_only():
    """Finding 2: a branch mutation must invalidate the cached listing for
    its codebase_dir immediately, rather than waiting out the TTL. Other
    codebase_dirs' cache entries must be untouched.
    """
    bs_mod._branches_cache.clear()
    svc_a = _svc(codebase="/repoA")
    svc_b = _svc(codebase="/repoB")
    calls = {"n": 0}

    def fake_uncached(self, include_remote=False):
        calls["n"] += 1
        return []

    with patch.object(BranchService, "_list_branches_uncached", fake_uncached):
        svc_a.list_branches()
        svc_b.list_branches()
        assert calls["n"] == 2

        BranchService.invalidate("/repoA")

        svc_a.list_branches()  # invalidated -> must re-walk
        assert calls["n"] == 3

        svc_b.list_branches()  # untouched -> must still hit cache
        assert calls["n"] == 3


def test_distinct_codebase_dirs_do_not_collide():
    bs_mod._branches_cache.clear()
    svc_a = _svc(codebase="/repoA")
    svc_b = _svc(codebase="/repoB")
    calls = {"n": 0}

    def fake_uncached(self, include_remote=False):
        calls["n"] += 1
        return [MagicMock(name=f"branch{calls['n']}")]

    with patch.object(BranchService, "_list_branches_uncached", fake_uncached):
        svc_a.list_branches()
        svc_b.list_branches()

    assert calls["n"] == 2  # each codebase_dir walked independently
    assert ("/repoA", False) in bs_mod._branches_cache
    assert ("/repoB", False) in bs_mod._branches_cache
