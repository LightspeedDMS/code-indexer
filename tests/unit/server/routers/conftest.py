"""
Shared pytest fixtures for server router unit tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _init_git_repo(repo_path: Path, content: str = "hello world\n") -> str:
    """Initialise a git repo at repo_path with hello.txt; return HEAD SHA."""
    subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(repo_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(repo_path), check=True, capture_output=True,
    )
    (repo_path / "hello.txt").write_text(content)
    subprocess.run(
        ["git", "add", "hello.txt"],
        cwd=str(repo_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(repo_path), check=True, capture_output=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path), check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def git_repo(tmp_path):
    """Create a real git repo with hello.txt; return (repo_path, head_sha)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    head_sha = _init_git_repo(repo)
    return repo, head_sha
