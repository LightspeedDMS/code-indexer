"""Conformance suite for the git invoker seam (``cidx_sdk.seams.git_invoker``).

Subclass :class:`GitInvokerContract` and provide the ``git_invoker`` fixture
(the invoker under test) and ``git_repo`` (a repository directory the invoker
can address). Checks the contract the core's ``run_git_command`` relies on:
a ``CompletedProcess`` with TEXT output, and a non-zero git exit REPORTED,
never raised.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cidx_sdk.seams.git_invoker import GitInvoker


def init_repo(path: Path) -> Path:
    """A one-commit repository at *path* (needs ``git`` on PATH)."""
    path.mkdir(parents=True, exist_ok=True)
    env_args = ["-c", "user.name=Example", "-c", "user.email=example@example.com"]
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("example\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(path), *env_args, "commit", "-q", "-m", "init"], check=True
    )
    return path


class GitInvokerContract:
    """The git invoker's contract. Subclass and provide both fixtures."""

    @pytest.fixture
    def git_invoker(self) -> GitInvoker:
        raise NotImplementedError("provide the invoker under test")

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return init_repo(tmp_path / "repo")

    def test_satisfies_the_protocol(self, git_invoker: GitInvoker) -> None:
        assert isinstance(git_invoker, GitInvoker)

    def test_a_successful_command_reports_text_output(
        self, git_invoker: GitInvoker, git_repo: Path
    ) -> None:
        done = git_invoker.run(["git", "rev-parse", "--is-inside-work-tree"], git_repo)
        assert isinstance(done, subprocess.CompletedProcess)
        assert done.returncode == 0
        assert isinstance(done.stdout, str) and done.stdout.strip() == "true"

    def test_a_failing_command_is_reported_not_raised(
        self, git_invoker: GitInvoker, git_repo: Path
    ) -> None:
        done = git_invoker.run(["git", "rev-parse", "no-such-ref"], git_repo)
        assert done.returncode != 0
        assert isinstance(done.stderr, str)

    def test_a_mutation_takes_effect_where_the_invoker_runs(
        self, git_invoker: GitInvoker, git_repo: Path
    ) -> None:
        done = git_invoker.run(
            ["git", "branch", "example-branch"], git_repo, timeout=60
        )
        assert done.returncode == 0, done.stderr
        listed = git_invoker.run(
            ["git", "branch", "--list", "example-branch"], git_repo
        )
        assert "example-branch" in listed.stdout
