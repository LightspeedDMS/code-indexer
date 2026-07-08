"""Tests for the get_branches bottleneck fix: list_branches is cached per
(repo working dir, include_remote) so repeated calls don't re-read a commit
object per ref (the ~13s O(refs) cost on many-branch repos)."""

import shutil
import tempfile
from pathlib import Path

from git import Repo

from code_indexer.services.git_topology_service import GitTopologyService
from code_indexer.server.services.branch_service import BranchService
from code_indexer.server.services import branch_service as bs


def _make_repo() -> str:
    d = tempfile.mkdtemp()
    repo = Repo.init(d)
    repo.config_writer().set_value("user", "name", "T").release()
    repo.config_writer().set_value("user", "email", "t@t.co").release()
    (Path(d) / "f.txt").write_text("x")
    repo.index.add(["f.txt"])
    repo.index.commit("init")
    return d


def test_list_branches_is_cached(monkeypatch):
    bs._branches_cache.clear()
    d = _make_repo()
    try:
        svc = BranchService(GitTopologyService(Path(d)))
        calls = {"n": 0}
        real = svc._list_branches_uncached

        def spy(include_remote=False):
            calls["n"] += 1
            return real(include_remote)

        monkeypatch.setattr(svc, "_list_branches_uncached", spy)

        first = svc.list_branches()
        second = svc.list_branches()
        assert calls["n"] == 1, "second call must reuse the cache, not re-walk refs"
        assert first is second

        # include_remote toggles the cache key -> a separate walk.
        svc.list_branches(include_remote=True)
        assert calls["n"] == 2
    finally:
        shutil.rmtree(d, ignore_errors=True)
        bs._branches_cache.clear()


def test_expired_branches_entry_rewalks(monkeypatch):
    bs._branches_cache.clear()
    d = _make_repo()
    try:
        svc = BranchService(GitTopologyService(Path(d)))
        calls = {"n": 0}
        real = svc._list_branches_uncached

        def spy(include_remote=False):
            calls["n"] += 1
            return real(include_remote)

        monkeypatch.setattr(svc, "_list_branches_uncached", spy)
        svc.list_branches()
        # Force-expire the entry.
        key = (str(svc.repo.working_dir), False)
        _exp, val = bs._branches_cache[key]
        bs._branches_cache[key] = (0.0, val)
        svc.list_branches()
        assert calls["n"] == 2, "an expired entry must re-walk"
    finally:
        shutil.rmtree(d, ignore_errors=True)
        bs._branches_cache.clear()
