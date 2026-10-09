"""Auto re-clone never destroys an index it cannot replace.

Re-clone clones into a sibling temp directory, then deletes the master and
renames the temp into place. On a pod that does not hold the repository the
master holds only ``.code-indexer``, and the clone lands with git-mcp on the
shared volume: the swap deleted the index, then failed to rename a directory
this pod cannot see. Pinned:

* under ``content_backend='delegated'`` re-clone is refused before anything is
  cloned or deleted, and says so at CRITICAL;
* whatever the config, a clone not visible on this pod is never swapped in --
  the master survives;
* a clone that IS here is still swapped in, as before.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, List

import pytest

from code_indexer.global_repos import refresh_scheduler as rs

URL = "https://git.example/agent-router.git"


class _Git:
    """Clones into ``where(dest)``: here, or somewhere this pod cannot see."""

    owns_credentials = True

    def __init__(self, where: Any) -> None:
        self.where = where
        self.cloned: List[str] = []

    def clone(self, url: str, dest: str, **kw: Any) -> None:
        self.cloned.append(dest)
        target = self.where(Path(dest))
        target.mkdir(parents=True)
        (target / "README.md").write_text("fresh clone\n")


@pytest.fixture
def master(tmp_path: Path) -> Path:
    m = tmp_path / "golden-repos" / "agent-router"
    (m / ".code-indexer").mkdir(parents=True)
    (m / ".code-indexer" / "chunks.db").write_text("the only copy of the index")
    return m


def _scheduler(git: _Git) -> Any:
    scheduler = rs.RefreshScheduler.__new__(rs.RefreshScheduler)
    scheduler._git_backend = git
    return scheduler


def test_delegated_content_refuses_before_cloning_or_deleting(
    master: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    monkeypatch.setattr(rs, "configured_content_backend_name", lambda: "delegated")
    git = _Git(where=lambda dest: dest)

    with caplog.at_level(logging.CRITICAL):
        ok = _scheduler(git)._attempt_reclone("agent-router-global", URL, str(master))

    assert ok is False
    assert git.cloned == [], "cloned anyway"
    assert (master / ".code-indexer" / "chunks.db").is_file(), "index deleted"
    assert any("SKIPPED" in r.getMessage() for r in caplog.records)


def test_a_clone_this_pod_cannot_see_is_never_swapped_in(
    master: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rs, "configured_content_backend_name", lambda: "local")
    elsewhere = tmp_path / "service-volume"
    git = _Git(where=lambda dest: elsewhere / dest.name)

    ok = _scheduler(git)._attempt_reclone("agent-router-global", URL, str(master))

    assert ok is False
    assert git.cloned, "the clone was not attempted"
    assert (master / ".code-indexer" / "chunks.db").is_file(), "index deleted"


def test_a_clone_that_is_here_is_still_swapped_in(
    master: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rs, "configured_content_backend_name", lambda: "local")
    git = _Git(where=lambda dest: dest)

    ok = _scheduler(git)._attempt_reclone("agent-router-global", URL, str(master))

    assert ok is True
    assert (master / "README.md").read_text() == "fresh clone\n"
    assert not (master.parent / ".reclone-agent-router-tmp").exists()


def test_an_unreadable_setting_refuses_rather_than_raising(
    master: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The method is documented "never raises"; and not knowing where the
    repository lives is no reason to delete anything."""

    def unavailable() -> str:
        raise RuntimeError("config service not initialised")

    monkeypatch.setattr(rs, "configured_content_backend_name", unavailable)
    git = _Git(where=lambda dest: dest)

    ok = _scheduler(git)._attempt_reclone("agent-router-global", URL, str(master))

    assert ok is False
    assert git.cloned == []
    assert (master / ".code-indexer" / "chunks.db").is_file()


def test_the_orphaned_clone_is_named_for_the_operator(
    master: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    monkeypatch.setattr(rs, "configured_content_backend_name", lambda: "local")
    git = _Git(where=lambda dest: tmp_path / "service-volume" / dest.name)

    with caplog.at_level(logging.CRITICAL):
        _scheduler(git)._attempt_reclone("agent-router-global", URL, str(master))

    orphan = str(master.parent / ".reclone-agent-router-tmp")
    assert any(
        "ORPHANED" in r.getMessage() and orphan in r.getMessage()
        for r in caplog.records
    )


def test_a_failed_clone_still_leaves_the_master(
    master: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rs, "configured_content_backend_name", lambda: "local")

    class _Failing(_Git):
        def clone(self, url: str, dest: str, **kw: Any) -> None:
            raise subprocess.CalledProcessError(128, "git clone", stderr="denied")

    ok = _scheduler(_Failing(where=lambda d: d))._attempt_reclone(
        "agent-router-global", URL, str(master)
    )

    assert ok is False
    assert (master / ".code-indexer" / "chunks.db").is_file()
