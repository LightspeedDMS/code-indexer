"""Unit tests for C6 rebalance pre-warming (ShardPrewarmService.run_once)."""

from typing import List

from code_indexer.server.services.shard_prewarm_service import ShardPrewarmService


class _Own:
    def __init__(self, owned):
        self._owned = set(owned)

    def owns(self, alias):
        return alias in self._owned


def test_warms_owned_repos_only_once():
    warmed: List[str] = []
    svc = ShardPrewarmService(_Own(["a", "b"]), lambda: ["a", "b", "c"], warmed.append)
    svc.run_once()
    assert sorted(warmed) == ["a", "b"]  # 'c' not owned -> not warmed
    svc.run_once()  # already warmed -> no repeat
    assert sorted(warmed) == ["a", "b"]


def test_membership_change_warms_newly_owned():
    warmed: List[str] = []
    own = _Own(["a"])
    svc = ShardPrewarmService(own, lambda: ["a", "b"], warmed.append)
    svc.run_once()
    assert warmed == ["a"]
    own._owned = {"a", "b"}  # membership shift -> now owns b
    svc.run_once()
    assert sorted(warmed) == ["a", "b"]


def test_drops_no_longer_owned_and_rewarms_on_return():
    warmed: List[str] = []
    own = _Own(["a"])
    svc = ShardPrewarmService(own, lambda: ["a"], warmed.append)
    svc.run_once()
    assert warmed == ["a"]
    own._owned = set()  # lost ownership
    svc.run_once()
    assert warmed == ["a"]  # nothing new; 'a' forgotten from warmed set
    own._owned = {"a"}  # regained
    svc.run_once()
    assert warmed == ["a", "a"]  # re-warmed


def test_solo_mode_is_noop():
    warmed: List[str] = []
    ShardPrewarmService(None, lambda: ["a"], warmed.append).run_once()
    assert warmed == []


def test_warm_failure_is_caught_and_retried_next_cycle():
    calls = []

    def warm(alias):
        calls.append(alias)
        raise RuntimeError("boom")

    svc = ShardPrewarmService(_Own(["a"]), lambda: ["a"], warm)
    svc.run_once()  # must not raise; 'a' not added to warmed
    assert calls == ["a"]
    svc.run_once()  # retried
    assert calls == ["a", "a"]


def test_repos_provider_error_is_caught():
    def boom():
        raise RuntimeError("db down")

    # Must not raise even if the repo listing fails.
    ShardPrewarmService(_Own(["a"]), boom, lambda a: None).run_once()
