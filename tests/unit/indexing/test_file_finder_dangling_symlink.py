"""Test that FileFinder.find_files skips dangling symlinks instead of crashing.

Regression test for the FTL golden-repo bug: Node.js cpSync converts relative
symlinks to absolute paths pointing at a temp clone dir. When the temp dir is
deleted the symlinks dangle, and ``file_path.stat()`` in find_files raised
``FileNotFoundError`` crashing the entire indexing job.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.indexing.file_finder import FileFinder


@pytest.fixture
def repo_with_dangling_symlink(tmp_path: Path) -> Path:
    """Create a minimal repo with a real file and a dangling symlink."""
    repo = tmp_path / "repo"
    repo.mkdir()

    # A normal file that should be indexed
    (repo / "README.md").write_text("# Hello")

    # A dangling symlink (target does not exist)
    dangling = repo / "AGENTS.md"
    os.symlink("/tmp/nonexistent-cidx-seed-dir/CLAUDE.md", str(dangling))

    # Verify setup
    assert dangling.is_symlink()
    assert not dangling.exists()  # dangling

    return repo


@pytest.fixture
def config(repo_with_dangling_symlink: Path) -> MagicMock:
    """Minimal Config mock for FileFinder."""
    cfg = MagicMock()
    cfg.codebase_dir = repo_with_dangling_symlink
    cfg.file_extensions = {"md", "py", "ts", "js"}
    cfg.gitignore_patterns = []
    cfg.indexing.max_file_size = 10_000_000
    # Disable override filtering
    cfg.override_config = None
    return cfg


def test_find_files_skips_dangling_symlink(
    repo_with_dangling_symlink: Path, config: MagicMock
) -> None:
    """find_files must skip dangling symlinks without raising."""
    finder = FileFinder(config)
    found = list(finder.find_files())

    # The real file should be found; the dangling symlink should be silently skipped
    found_names = {f.name for f in found}
    assert "README.md" in found_names
    assert "AGENTS.md" not in found_names


def test_find_files_includes_valid_symlink(tmp_path: Path) -> None:
    """Valid (non-dangling) symlinks should still be indexed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("# Instructions")
    os.symlink("CLAUDE.md", str(repo / "AGENTS.md"))

    # Verify setup: symlink resolves
    assert (repo / "AGENTS.md").exists()

    cfg = MagicMock()
    cfg.codebase_dir = repo
    cfg.file_extensions = {"md"}
    cfg.gitignore_patterns = []
    cfg.indexing.max_file_size = 10_000_000
    cfg.override_config = None

    finder = FileFinder(cfg)
    found_names = {f.name for f in finder.find_files()}

    assert "CLAUDE.md" in found_names
    assert "AGENTS.md" in found_names
