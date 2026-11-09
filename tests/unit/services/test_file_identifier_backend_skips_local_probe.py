"""With a git backend, FileIdentifier never probes LOCAL git.

``FileIdentifier.__init__`` used to call ``_detect_git()`` -- a local
``git rev-parse`` through ``utils.git_runner.is_git_repository`` -- before the
git-backend probe overwrote the answer. On a delegated pod that forked git
against a path the pod does not hold, once per FileIdentifier. With a backend
the backend answers; without one the local probe is unchanged.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List

import pytest

import code_indexer.services.file_identifier as file_identifier_module
from code_indexer.services.file_identifier import FileIdentifier

SHA = "ab" * 20


class _GitMcp:
    def __init__(self, *, not_a_repo: bool = False) -> None:
        self.not_a_repo = not_a_repo
        self.calls: List[str] = []

    def rev_parse(self, repo_path: str, *args: str, **kw: Any) -> str:
        self.calls.append("rev_parse")
        if self.not_a_repo:
            raise subprocess.CalledProcessError(
                128, "git-mcp", stderr="fatal: not a git repository"
            )
        return SHA


@pytest.fixture
def no_local_git(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"local git/subprocess under a git backend: {args!r}")

    monkeypatch.setattr(file_identifier_module, "is_git_repository", _forbidden)
    monkeypatch.setattr(file_identifier_module, "run_git_command", _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)


def test_backend_repository_is_detected_without_local_git(
    tmp_path: Path, no_local_git: None
) -> None:
    git = _GitMcp()
    ident = FileIdentifier(tmp_path, git_backend=git)
    assert ident.git_available is True
    assert git.calls == ["rev_parse"]


def test_backend_non_repository_is_detected_without_local_git(
    tmp_path: Path, no_local_git: None
) -> None:
    ident = FileIdentifier(tmp_path, git_backend=_GitMcp(not_a_repo=True))
    assert ident.git_available is False


def test_without_backend_the_local_probe_still_answers(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    assert FileIdentifier(repo).git_available is True
    assert FileIdentifier(plain).git_available is False


def test_without_backend_detection_goes_through_is_git_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: List[Path] = []

    def _probe(path: Path) -> bool:
        seen.append(path)
        return True

    monkeypatch.setattr(file_identifier_module, "is_git_repository", _probe)
    assert FileIdentifier(tmp_path).git_available is True
    assert seen == [tmp_path]
