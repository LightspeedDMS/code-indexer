"""Tests for Bug #368: DirectoryExplorerService should not crash on PermissionError/OSError.

Broken symlinks and inaccessible filesystem entries can raise PermissionError or
OSError when stat'd (e.g., entry.is_dir(), entry.is_file()). The service must
skip such entries silently rather than propagating the exception.
"""

import os

import pytest

from code_indexer.global_repos.directory_explorer import (
    DirectoryExplorerService,
    DirectoryTreeResult,
)


@pytest.fixture
def repo_with_normal_file(tmp_path):
    """Create a minimal repo with one normal file."""
    (tmp_path / "normal.txt").write_text("hello")
    return tmp_path


def test_generate_tree_skips_dangling_symlink(tmp_path):
    """Bug #368: generate_tree must not crash on a dangling (broken) symlink.

    A dangling symlink raises OSError when entry.is_dir() or entry.is_file() is
    called because the target does not exist. The entry must be silently skipped.
    """
    (tmp_path / "normal.txt").write_text("hello")
    # Dangling symlink: target does not exist
    os.symlink("/nonexistent/restricted/file", str(tmp_path / "broken_link"))

    service = DirectoryExplorerService(tmp_path)
    result = service.generate_tree()

    assert isinstance(result, DirectoryTreeResult)
    assert result.root is not None
    # The normal file should appear in the tree string
    assert "normal.txt" in result.tree_string


def test_generate_tree_counts_correctly_after_skipping_symlink(tmp_path):
    """Bug #368: file count should reflect only accessible entries after skipping broken symlinks."""
    (tmp_path / "a.py").write_text("a")
    (tmp_path / "b.py").write_text("b")
    os.symlink("/nonexistent/path", str(tmp_path / "broken_link"))

    service = DirectoryExplorerService(tmp_path)
    result = service.generate_tree()

    # 2 real files; broken symlink must be silently ignored (not raise, not counted)
    assert result.total_files == 2


def test_generate_tree_skips_symlink_to_restricted_device(tmp_path):
    """Bug #368: generate_tree must not crash on symlink to a restricted device file.

    On Linux /dev/null is always accessible, but we test with a path that simulates
    what happens when stat raises PermissionError by creating a broken symlink. The
    key invariant is: no exception must propagate.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "code.py").write_text("pass")
    # Broken symlink inside subdirectory
    os.symlink("/proc/1/mem", str(tmp_path / "src" / "restricted_link"))

    service = DirectoryExplorerService(tmp_path)
    # Must not raise - that is the primary assertion
    result = service.generate_tree()
    assert result is not None


def test_generate_tree_inaccessible_directory_skipped(tmp_path):
    """Bug #368: generate_tree must not crash when a subdirectory is inaccessible (chmod 000).

    This covers PermissionError raised by os.scandir() on a restricted directory.
    The inaccessible directory is skipped; remaining content is returned normally.
    """
    (tmp_path / "normal.txt").write_text("hello")
    restricted = tmp_path / "restricted_dir"
    restricted.mkdir()
    (restricted / "secret.txt").write_text("secret")
    restricted.chmod(0o000)

    try:
        service = DirectoryExplorerService(tmp_path)
        result = service.generate_tree()

        assert result is not None
        assert "normal.txt" in result.tree_string
    finally:
        # Restore permissions so pytest tmp_path cleanup works
        restricted.chmod(0o755)


def test_generate_tree_multiple_broken_symlinks(tmp_path):
    """Bug #368: generate_tree handles multiple broken symlinks in the same directory."""
    (tmp_path / "real.py").write_text("real")
    for i in range(5):
        os.symlink(f"/nonexistent/path_{i}", str(tmp_path / f"broken_{i}"))

    service = DirectoryExplorerService(tmp_path)
    result = service.generate_tree()

    assert result is not None
    assert "real.py" in result.tree_string
    assert result.total_files == 1
