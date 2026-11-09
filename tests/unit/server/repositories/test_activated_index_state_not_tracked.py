"""issue-64616: an activated repository never shows or stages ``.code-indexer/``.

On tools-test ``git_status`` of a freshly activated copy listed
``.code-indexer/`` as untracked, so a stage-all could commit cidx's index
state. Now:

* activation keeps it out of git: locally ``/.code-indexer/`` is added to
  ``.git/info/exclude`` (once); under delegated -- workspace-mcp never writes
  under ``.git`` and git-mcp has no exclude tool -- a ``.gitignore`` of ``*``
  inside ``.code-indexer/`` is written through the content writer;
* as a backstop for copies activated before this, ``git_status`` never
  reports it untracked and ``git_stage`` never stages it, even for ``.``.

Real git repositories throughout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from code_indexer.server.repositories import activated_working_copy as awc
from code_indexer.server.repositories.activated_working_copy import (
    INDEX_STATE_EXCLUDE_LINE,
    INDEX_STATE_GITIGNORE_BODY,
    DelegatedWorkingCopies,
    LocalWorkingCopies,
)
from code_indexer.server.services.git_operations_service import (
    GitOperationsService,
    is_index_state_path,
)
from code_indexer.server.startup import content_writer_wiring
from tests.unit.global_repos.test_refresh_git_cancel_2012 import REAL_GIT
from tests.unit.global_repos.test_refresh_registered_branch_2148 import git_out

pytestmark = pytest.mark.skipif(REAL_GIT is None, reason="git binary not installed")


@pytest.fixture(autouse=True)
def _no_user_git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's global excludes (often ``.code-indexer``) must not decide
    these tests."""
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _activated_copy(tmp_path: Path) -> Path:
    repo = tmp_path / "admin" / "e64616-fd"
    git_out("init", "-b", "main", str(repo), cwd=tmp_path)
    (repo / "a.py").write_text("x = 1\n")
    git_out("add", "-A", cwd=repo)
    git_out("commit", "-m", "first", cwd=repo)
    (repo / ".code-indexer" / "index").mkdir(parents=True)
    (repo / ".code-indexer" / "config.json").write_text("{}")
    (repo / ".code-indexer" / "index" / "chunks.db").write_bytes(b"\0")
    return repo


def _untracked(repo: Path) -> List[str]:
    out = git_out("status", "--porcelain=v1", "--untracked-files=all", cwd=repo)
    return [line[3:] for line in out.splitlines() if line.startswith("??")]


def test_local_activation_excludes_the_index_state_once(tmp_path: Path) -> None:
    repo = _activated_copy(tmp_path)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("# existing\n*.log")  # no trailing newline
    assert any(p.startswith(".code-indexer/") for p in _untracked(repo))

    LocalWorkingCopies().exclude_index_state(repo)
    LocalWorkingCopies().exclude_index_state(repo)

    assert _untracked(repo) == []
    assert exclude.read_text().splitlines() == [
        "# existing",
        "*.log",
        INDEX_STATE_EXCLUDE_LINE,
    ]


def test_local_activation_without_a_git_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        LocalWorkingCopies().exclude_index_state(tmp_path)


#: exclude_index_state never touches the clone-path store.
_NO_STORE: Any = object()


class _Writer:
    def __init__(self) -> None:
        self.puts: List[Tuple[str, bytes]] = []

    def put(self, path: str, data: bytes, **kw: Any) -> str:
        self.puts.append((path, data))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(data)  # workspace-mcp's copy is this disk here
        return "sha"


def test_delegated_activation_writes_a_self_ignoring_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _activated_copy(tmp_path)
    writer = _Writer()
    monkeypatch.setattr(content_writer_wiring, "server_content_writer", lambda: writer)

    DelegatedWorkingCopies(store=_NO_STORE).exclude_index_state(repo)

    assert writer.puts == [
        (str(repo / ".code-indexer" / ".gitignore"), INDEX_STATE_GITIGNORE_BODY)
    ]
    assert _untracked(repo) == [], "git still sees the index state"
    assert (
        not (repo / ".git" / "info" / "exclude")
        .read_text()
        .count(INDEX_STATE_EXCLUDE_LINE)
    )


def test_delegated_activation_without_a_writer_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(content_writer_wiring, "server_content_writer", lambda: None)
    with pytest.raises(awc.WorkingCopyUnavailableError):
        DelegatedWorkingCopies(store=_NO_STORE).exclude_index_state(tmp_path)


# ── backstop for copies activated before the fix ─────────────────────────────


def test_git_status_never_reports_the_index_state(tmp_path: Path) -> None:
    repo = _activated_copy(tmp_path)
    (repo / "new.py").write_text("y = 2\n")

    status = GitOperationsService().git_status(repo)

    assert status["untracked"] == ["new.py"]


def test_stage_all_never_stages_the_index_state(tmp_path: Path) -> None:
    repo = _activated_copy(tmp_path)
    (repo / "new.py").write_text("y = 2\n")

    GitOperationsService().git_stage(repo, ["."])

    staged = git_out("diff", "--cached", "--name-only", cwd=repo).splitlines()
    assert staged == ["new.py"]
    # The stage excluded the index state first (and again is a no-op).
    exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count(INDEX_STATE_EXCLUDE_LINE) == 1
    GitOperationsService().git_stage(repo, ["."])
    assert git_out("diff", "--cached", "--name-only", cwd=repo).splitlines() == [
        "new.py"
    ]


def test_staging_one_file_does_not_touch_the_exclude(tmp_path: Path) -> None:
    repo = _activated_copy(tmp_path)
    (repo / "new.py").write_text("y = 2\n")
    exclude = repo / ".git" / "info" / "exclude"
    before = exclude.read_text() if exclude.exists() else ""

    GitOperationsService().git_stage(repo, ["new.py"])

    assert (exclude.read_text() if exclude.exists() else "") == before


def test_a_stage_of_the_tree_that_cannot_exclude_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.services.git_operations_service import GitCommandError

    repo = _activated_copy(tmp_path)

    def broken(path: Any) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(awc, "exclude_index_state_locally", broken)
    with pytest.raises(GitCommandError, match="could not exclude"):
        GitOperationsService().git_stage(repo, ["."])
    assert git_out("diff", "--cached", "--name-only", cwd=repo) == ""


@pytest.mark.parametrize("path", [".code-indexer", "./.code-indexer", ".code-indexer/"])
def test_staging_the_index_state_by_name_is_refused(tmp_path: Path, path: str) -> None:
    repo = _activated_copy(tmp_path)
    with pytest.raises(ValueError, match="Cannot stage .code-indexer"):
        GitOperationsService().git_stage(repo, [path])
    assert git_out("diff", "--cached", "--name-only", cwd=repo) == ""


def test_is_index_state_path() -> None:
    assert is_index_state_path(".code-indexer/index/x")
    assert is_index_state_path("./.code-indexer")
    assert not is_index_state_path("src/.code-indexer-notes.md")
    assert not is_index_state_path(".code-indexer-override.yaml")


def test_unborn_repo_stage_still_works(tmp_path: Path) -> None:
    repo = tmp_path / "fresh"
    git_out("init", "-b", "main", str(repo), cwd=tmp_path)
    (repo / "a.py").write_text("x\n")
    (repo / ".code-indexer").mkdir()
    (repo / ".code-indexer" / "c.json").write_text("{}")

    GitOperationsService().git_stage(repo, ["."])

    staged = subprocess.run(
        [str(REAL_GIT), "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert staged == ["a.py"]
