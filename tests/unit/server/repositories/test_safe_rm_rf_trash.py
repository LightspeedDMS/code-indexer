"""Safety tests for _safe_purge_trash_entry (Story #1032 AC7 / AC8).

After Codex GPT-5 code review found a TOCTOU vulnerability in the previous
path-based subprocess design, the implementation was rewritten to use
fd-anchored Python deletion. These tests lock in the new safety invariants.

Specifically the new design ELIMINATES the path-validation surface by only
accepting a `trash_root` directory + a basename `entry_name`. All filesystem
ops happen relative to file descriptors opened with O_DIRECTORY|O_NOFOLLOW,
so an ancestor symlink swap cannot redirect the recursion.
"""

import os
import tempfile
from pathlib import Path

import pytest

from src.code_indexer.server.repositories.activated_repo_manager import (
    _safe_purge_trash_entry,
)


@pytest.fixture
def trash_root():
    """Create a real .trash directory and yield its absolute path."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / ".trash"
        root.mkdir(parents=True, exist_ok=True)
        yield str(root)


def _make_entry(trash_root: str, name: str) -> str:
    """Create a populated entry under trash_root and return its absolute path."""
    p = Path(trash_root) / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "file.txt").write_text("delete me")
    nested = p / "sub" / "deep"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "nested.txt").write_text("nested data")
    return str(p)


# -------- ValueError safety invariants (BEFORE any FS ops) -------------------


class TestEntryNameSafety:
    """entry_name validation must reject obviously-bad inputs."""

    def test_rejects_empty_entry_name(self, trash_root):
        with pytest.raises(ValueError, match="empty"):
            _safe_purge_trash_entry(trash_root, "")

    def test_rejects_none_entry_name(self, trash_root):
        with pytest.raises(ValueError):
            _safe_purge_trash_entry(trash_root, None)  # type: ignore[arg-type]

    def test_rejects_entry_name_with_forward_slash(self, trash_root):
        with pytest.raises(ValueError, match="separator"):
            _safe_purge_trash_entry(trash_root, "foo/bar")

    def test_rejects_entry_name_with_backslash(self, trash_root):
        with pytest.raises(ValueError, match="separator"):
            _safe_purge_trash_entry(trash_root, "foo\\bar")

    def test_rejects_entry_name_dot(self, trash_root):
        with pytest.raises(ValueError, match="\\.|dot"):
            _safe_purge_trash_entry(trash_root, ".")

    def test_rejects_entry_name_dotdot(self, trash_root):
        with pytest.raises(ValueError, match="\\.|dot"):
            _safe_purge_trash_entry(trash_root, "..")

    def test_rejects_entry_name_dotdot_prefix(self, trash_root):
        with pytest.raises(ValueError, match="\\.|dot"):
            _safe_purge_trash_entry(trash_root, "..evil")

    def test_rejects_entry_name_with_null_byte(self, trash_root):
        with pytest.raises(ValueError, match="null"):
            _safe_purge_trash_entry(trash_root, "foo\x00bar")


class TestTrashRootSafety:
    """trash_root validation must reject empty/non-string and symlinked roots."""

    def test_rejects_empty_trash_root(self):
        with pytest.raises(ValueError, match="trash_root"):
            _safe_purge_trash_entry("", "foo")

    def test_rejects_none_trash_root(self):
        with pytest.raises(ValueError):
            _safe_purge_trash_entry(None, "foo")  # type: ignore[arg-type]

    def test_refuses_symlinked_trash_root(self, tmp_path):
        # Real trash root + a symlink to it.  Function must refuse to open
        # the symlink (O_NOFOLLOW), so even with a valid entry name we get ValueError.
        real_root = tmp_path / "real_trash"
        real_root.mkdir()
        (real_root / "entry").mkdir()
        link = tmp_path / "linked_trash"
        os.symlink(str(real_root), str(link))
        with pytest.raises(ValueError, match="trash_root"):
            _safe_purge_trash_entry(str(link), "entry")


# -------- Symlink-NOT-followed (defense in depth) ----------------------------


class TestSymlinkNeverFollowed:
    """An entry that is itself a symlink must be unlinked, never recursed into."""

    def test_symlink_entry_unlinked_target_preserved(self, trash_root, tmp_path):
        # Create a target directory OUTSIDE the trash root with sensitive content.
        outside_target = tmp_path / "outside_target"
        outside_target.mkdir()
        (outside_target / "do_not_delete.txt").write_text("important")

        # Create a symlink under trash root pointing to it.
        link_path = Path(trash_root) / "evil_link"
        os.symlink(str(outside_target), str(link_path))

        # Purge the entry by name.
        _safe_purge_trash_entry(trash_root, "evil_link")

        # The symlink should be gone.
        assert not link_path.exists() and not link_path.is_symlink()
        # The target should be UNTOUCHED.
        assert outside_target.exists()
        assert (outside_target / "do_not_delete.txt").read_text() == "important"


# -------- Cross-filesystem boundary refusal ----------------------------------


class TestCrossFilesystemRefusal:
    """If the entry is on a different filesystem than the trash root, refuse."""

    def test_rejects_cross_fs_entry(self, trash_root, monkeypatch):
        entry = _make_entry(trash_root, "cross-fs-test")

        real_fstat = os.fstat

        def fake_fstat(fd):
            # Return st_dev=99 for the entry (any non-root fd), st_dev=1 for root.
            st = real_fstat(fd)

            class FakeStat:
                def __getattr__(self, name):
                    return getattr(st, name)

                st_dev = 99 if fd != root_fds[0] else 1

            return FakeStat()

        # Open trash root manually so we know which fd is root vs entry.
        root_fds: list[int] = []
        original_open = os.open

        def tracking_open(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            # The first open with O_DIRECTORY|O_NOFOLLOW is for root.
            if (
                len(args) >= 2
                and (args[1] & os.O_DIRECTORY)
                and (args[1] & os.O_NOFOLLOW)
                and "dir_fd" not in kwargs
                and not root_fds
            ):
                root_fds.append(fd)
            return fd

        monkeypatch.setattr(os, "open", tracking_open)
        monkeypatch.setattr(os, "fstat", fake_fstat)

        with pytest.raises(ValueError, match="cross-filesystem"):
            _safe_purge_trash_entry(trash_root, "cross-fs-test")

        # Entry should still exist (rejected before unlink)
        assert os.path.exists(entry)


# -------- Real recursive deletion --------------------------------------------


class TestRealDeletion:
    """Actually delete a populated trash entry end-to-end."""

    def test_deletes_populated_directory_tree(self, trash_root):
        entry = _make_entry(trash_root, "victim-1")
        assert os.path.exists(entry)
        assert os.path.exists(os.path.join(entry, "sub", "deep", "nested.txt"))

        _safe_purge_trash_entry(trash_root, "victim-1")

        assert not os.path.exists(entry)

    def test_deletes_single_file_entry(self, trash_root):
        # Plain file (not a directory) under trash root.
        file_entry = Path(trash_root) / "loose_file.txt"
        file_entry.write_text("just a file")

        _safe_purge_trash_entry(trash_root, "loose_file.txt")

        assert not file_entry.exists()

    def test_nonexistent_entry_is_noop(self, trash_root):
        # Should NOT raise FileNotFoundError; absorbed by inner unlink fallback.
        _safe_purge_trash_entry(trash_root, "does-not-exist")
