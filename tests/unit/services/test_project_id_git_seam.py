"""The project id comes from the git seam on a pod without the repository.

``FileIdentifier.get_project_id`` names a repository after its origin remote,
and that id seeds every chunk's point id and is compared by
``ProgressiveMetadata.should_force_full_index``. It used to ask LOCAL git even
when a git backend was configured; on a pod holding only ``.code-indexer`` that
fails and the id silently became the directory name -- a different id from the
one the same repository gets with its checkout present, so every point id
changed and the next run was forced to a full re-index.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List

import pytest

from code_indexer.services.file_identifier import FileIdentifier

SHA = "c0ffee" + "0" * 34


class _GitMcp:
    """git-mcp answering for a repository this pod does not hold."""

    owns_credentials = True

    def __init__(self, *, origin: Any = "git@gitlab.example:org/Agent_Router.git"):
        self.origin = origin
        self.calls: List[str] = []

    def rev_parse(self, repo_path: str, *args: str, **kw: Any) -> str:
        return SHA

    def remote_get_url(self, repo_path: str, name: str, **kw: Any) -> str:
        self.calls.append(f"remote_get_url {name}")
        if isinstance(self.origin, BaseException):
            raise self.origin
        return str(self.origin)


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    """What a mount-less pod holds at the golden repo's path: no .git."""
    repo = tmp_path / "golden-repos" / "router-alias"
    (repo / ".code-indexer").mkdir(parents=True)
    return repo


def test_project_id_is_the_origin_name_from_the_backend(empty_repo: Path) -> None:
    git = _GitMcp()
    assert FileIdentifier(empty_repo, git_backend=git).get_project_id() == (
        "agent-router"
    )
    assert git.calls == ["remote_get_url origin"]


def test_no_origin_remote_falls_back_to_the_directory_name(empty_repo: Path) -> None:
    no_remote = subprocess.CalledProcessError(
        2, "git-mcp", stderr="error: No such remote 'origin'"
    )
    git = _GitMcp(origin=no_remote)
    assert FileIdentifier(empty_repo, git_backend=git).get_project_id() == (
        "router-alias"
    )


def test_a_transport_failure_is_not_read_as_no_remote(empty_repo: Path) -> None:
    """Falling back would re-identify every chunk; failing is the safe side."""
    git = _GitMcp(origin=ConnectionError("git-mcp unreachable"))
    with pytest.raises(ConnectionError):
        FileIdentifier(empty_repo, git_backend=git).get_project_id()


def test_without_a_backend_local_git_answers(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example/x/My_Lib.git"],
        cwd=repo,
        check=True,
    )
    assert FileIdentifier(repo).get_project_id() == "my-lib"
