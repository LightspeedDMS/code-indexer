"""The local git backend passes the git seam's conformance suite."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple, cast

import pytest

from cidx_backend_git.local import LocalGitBackend, make_git_backend
from cidx_sdk.seams.git import GitCommandRunner, GitProcessRunner, Timeout
from cidx_testkit.conformance.git import (
    GitBackendContract,
    GitSubject,
    PlainGitProcessRunner,
    local_subject,
)


class TestLocalGitBackend(GitBackendContract):
    @pytest.fixture
    def git_subject(self, tmp_path: Path) -> GitSubject:
        return local_subject(make_git_backend(PlainGitProcessRunner()), tmp_path)


class _Answering:
    """A runner answering every command with one fixed result, recording it."""

    def __init__(self, returncode: int, stdout: Any, stderr: Any) -> None:
        self.result = (returncode, stdout, stderr)
        self.calls: List[Tuple[List[str], dict]] = []

    def run_git(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout: Timeout = None,
        retry: bool = False,
        text: bool = True,
        credentials_url: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> "subprocess.CompletedProcess[Any]":
        self.calls.append(
            (
                list(cmd),
                {"cwd": cwd, "retry": retry, "credentials_url": credentials_url},
            )
        )
        code, out, err = self.result
        return subprocess.CompletedProcess(list(cmd), code, out, err)


def test_the_entry_point_factory_builds_a_local_backend() -> None:
    runner = PlainGitProcessRunner()
    backend = make_git_backend(runner)
    assert isinstance(backend, LocalGitBackend)
    assert isinstance(backend, GitCommandRunner)
    assert isinstance(runner, GitProcessRunner)
    assert backend.runner is runner
    assert backend.owns_credentials is False


def test_a_runner_is_required() -> None:
    with pytest.raises(TypeError, match="GitProcessRunner"):
        LocalGitBackend(cast(GitProcessRunner, object()))


def test_network_operations_hand_the_runner_retry_and_credentials(
    tmp_path: Path,
) -> None:
    """How git is spawned is the runner's: the backend passes it the caller's
    retry choice and credential instead of acting on them itself."""
    runner = _Answering(0, "", "")
    backend = make_git_backend(runner)
    backend.fetch(str(tmp_path), retry=True, credentials_url="https://example.com/r")
    cmd, kwargs = runner.calls[-1]
    assert cmd == ["git", "fetch", "origin"]
    assert kwargs == {
        "cwd": tmp_path,
        "retry": True,
        "credentials_url": "https://example.com/r",
    }


def test_a_failure_redacts_url_credentials(tmp_path: Path) -> None:
    secret = "https://user:example-secret@example.com/r.git"
    runner = _Answering(128, f"{secret}\n".encode(), f"fatal: {secret}".encode())
    backend = make_git_backend(runner)
    with pytest.raises(subprocess.CalledProcessError) as raised:
        backend.log_raw(str(tmp_path), "%H")
    assert b"example-secret" not in raised.value.output
    assert "example-secret" not in raised.value.stderr
    assert "example.com/r.git" in raised.value.stderr
