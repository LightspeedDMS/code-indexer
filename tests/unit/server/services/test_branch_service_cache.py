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
