"""Confined repository paths with a ``.git`` segment are refused.

The rule (``code_indexer.utils.path_confinement``): after resolution
(``..`` collapsed, symlinks followed), a path whose repository-relative
location has a segment equal to ``.git`` (compared case-insensitively) is
refused with ``GitDirectoryPathError``, a ``PermissionError``. Names that
merely start with ``.git`` are ordinary paths.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from code_indexer.utils.path_confinement import (
    GitDirectoryPathError,
    has_git_segment,
    is_readable_within_root,
    resolve_confined_path,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text("[core]\n")
    (root / "sub" / ".git").mkdir(parents=True)
    (root / ".github").mkdir()
    (root / ".github" / "ci.yml").write_text("name: ci\n")
    (root / ".gitignore").write_text("*.pyc\n")
    (root / "README.md").write_text("example\n")
    os.symlink(".git", root / "gitlink")
    return root


@pytest.mark.parametrize(
    "relative",
    [
        ".git",
        ".git/",
        ".git/config",
        "./.git/config",
        "docs/../.git/config",
        ".GIT/config",
        "gitlink/config",
        "sub/.git/HEAD",
    ],
)
def test_git_paths_are_refused(repo: Path, relative: str) -> None:
    with pytest.raises(GitDirectoryPathError) as raised:
        resolve_confined_path(repo, relative)
    assert isinstance(raised.value, PermissionError)


@pytest.mark.parametrize(
    "relative", [".gitignore", ".github/ci.yml", ".git/../README.md", "README.md"]
)
def test_other_paths_are_confined_normally(repo: Path, relative: str) -> None:
    resolved = resolve_confined_path(repo, relative)
    assert resolved.relative_to(repo.resolve())


@pytest.mark.parametrize(
    "relative,expected",
    [
        ("gitlink", False),
        ("link.py", False),
        ("sub/.git/HEAD", False),
        ("out.py", False),
        ("README.md", True),
        (".gitignore", True),
        (".github/ci.yml", True),
        ("ok.md", True),
    ],
)
def test_is_readable_within_root(
    repo: Path, tmp_path: Path, relative: str, expected: bool
) -> None:
    """Judged on the RESOLVED location: a name with no .git segment that
    resolves into .git, or outside the root, is not readable."""
    (repo / "link.py").symlink_to(".git/config")
    (repo / "ok.md").symlink_to("README.md")
    (repo / "sub" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / "outside.py").write_text("x = 1\n")
    (repo / "out.py").symlink_to(tmp_path / "outside.py")
    assert is_readable_within_root(repo / relative, repo.resolve()) is expected


@pytest.mark.parametrize(
    "parts,expected",
    [
        ((".git",), True),
        (("a", ".Git", "b"), True),
        ((".gitignore",), False),
        ((".github", "x"), False),
        (("git",), False),
        ((), False),
    ],
)
def test_has_git_segment(parts: tuple, expected: bool) -> None:
    assert has_git_segment(parts) is expected
