"""EVO-64616: the post-clone workflow waits for a DELEGATED clone to become
visible on THIS node before spawning `cidx init`/`cidx index` children.

A delegated clone is written by git-mcp on a DIFFERENT NFS client, so this cidx
process's client can briefly negative-cache the just-created directory even after
the backend reports success — a child `subprocess(cwd=clone_path)` then dies with
FileNotFoundError on the cwd. `_await_clone_path_locally_visible` opens the dir
(forcing lookup + readdir) with a bounded retry to refresh the local caches first.
"""

from __future__ import annotations

import pytest

from code_indexer.server.repositories import golden_repo_manager as grm
from code_indexer.server.repositories.golden_repo_manager import (
    GitOperationError,
    GoldenRepoManager,
)


def _mgr(tmp_path) -> GoldenRepoManager:
    return GoldenRepoManager(data_dir=str(tmp_path))


class TestCloneVisibilityBarrier:
    def test_no_op_when_already_visible(self, tmp_path, monkeypatch):
        m = _mgr(tmp_path)
        d = tmp_path / "repo"
        d.mkdir()
        slept: list = []
        monkeypatch.setattr(grm.time, "sleep", lambda s: slept.append(s))

        m._await_clone_path_locally_visible(str(d))

        # Local clone: visible first try -> never sleeps.
        assert slept == []

    def test_retries_then_succeeds(self, tmp_path, monkeypatch):
        m = _mgr(tmp_path)
        d = tmp_path / "repo"
        d.mkdir()
        real_scandir = grm.os.scandir
        slept: list = []
        monkeypatch.setattr(grm.time, "sleep", lambda s: slept.append(s))

        n = {"c": 0}

        def flaky(path):
            n["c"] += 1
            if n["c"] <= 3:  # negative-cached for the first 3 checks
                raise FileNotFoundError(2, "No such file or directory", str(path))
            return real_scandir(path)

        monkeypatch.setattr(grm.os, "scandir", flaky)

        m._await_clone_path_locally_visible(str(d))

        assert n["c"] == 4  # 3 misses + 1 success
        assert len(slept) == 3  # backed off between each miss

    def test_raises_when_never_visible(self, tmp_path, monkeypatch):
        m = _mgr(tmp_path)
        monkeypatch.setattr(grm.time, "sleep", lambda s: None)

        def always_missing(path):
            raise FileNotFoundError(2, "No such file or directory", str(path))

        monkeypatch.setattr(grm.os, "scandir", always_missing)

        with pytest.raises(GitOperationError, match="never became visible"):
            m._await_clone_path_locally_visible(
                str(tmp_path / "missing"), attempts=3, delay_seconds=0.0
            )
