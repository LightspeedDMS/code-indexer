"""Tests for fd-anchored Phase 1 rename in _do_deactivate_single (Story #1032 Commit 5).

BLOCKERS #1 and #2 from Codex re-review:
  - BLOCKER #1: .trash dir can be swapped to a symlink between makedirs and rename.
  - BLOCKER #2: {username} ancestor dir can be swapped to a symlink before rename.

These tests assert that `_fd_anchored_phase1_rename` (new helper) pins both
parent dirs by opening them with O_DIRECTORY|O_NOFOLLOW before calling
os.rename with dual dir_fd parameters.  An attacker swapping .trash or the
username dir cannot redirect the rename to a path outside activated_repos_dir.
"""

import os
from pathlib import Path

import pytest

from src.code_indexer.server.repositories.activated_repo_manager import (
    _fd_anchored_phase1_rename,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def arena(tmp_path):
    """Build a minimal activated_repos_dir layout.

    Layout:
        arena/
            activated_repos/
                .trash/
                alice/
                    my-repo/    (the repo to be deactivated)
                        somefile.txt
    """
    activated_repos_dir = tmp_path / "activated_repos"
    trash = activated_repos_dir / ".trash"
    user_dir = activated_repos_dir / "alice"
    repo_dir = user_dir / "my-repo"

    trash.mkdir(parents=True)
    repo_dir.mkdir(parents=True)
    (repo_dir / "somefile.txt").write_text("data")

    return {
        "activated_repos_dir": str(activated_repos_dir),
        "trash": str(trash),
        "user_dir": str(user_dir),
        "repo_dir": str(repo_dir),
    }


# ---------------------------------------------------------------------------
# Happy-path: rename succeeds, repo lands in .trash
# ---------------------------------------------------------------------------


class TestFdAnchoredRenameSuccess:
    """The rename must place the repo under the real .trash inode."""

    def test_repo_disappears_from_user_dir(self, arena):
        _fd_anchored_phase1_rename(
            activated_repos_dir=arena["activated_repos_dir"],
            username="alice",
            user_alias="my-repo",
        )
        assert not os.path.exists(arena["repo_dir"]), "repo must be gone from user dir"

    def test_repo_appears_in_real_trash(self, arena):
        trash_name = _fd_anchored_phase1_rename(
            activated_repos_dir=arena["activated_repos_dir"],
            username="alice",
            user_alias="my-repo",
        )
        trash_entry = Path(arena["trash"]) / trash_name
        assert trash_entry.exists(), "repo must appear in .trash"
        assert (trash_entry / "somefile.txt").exists(), "file content preserved"

    def test_returns_nonempty_basename(self, arena):
        trash_name = _fd_anchored_phase1_rename(
            activated_repos_dir=arena["activated_repos_dir"],
            username="alice",
            user_alias="my-repo",
        )
        assert trash_name, "must return a non-empty trash_name basename"
        assert "/" not in trash_name, "must be a basename (no slashes)"
        assert "\\" not in trash_name, "must be a basename (no backslashes)"

    def test_trash_name_contains_user_alias(self, arena):
        trash_name = _fd_anchored_phase1_rename(
            activated_repos_dir=arena["activated_repos_dir"],
            username="alice",
            user_alias="my-repo",
        )
        assert "my-repo" in trash_name, (
            "trash_name should embed user_alias for debuggability"
        )

    def test_renamed_entry_lives_on_same_inode_as_trash(self, arena):
        """Verify via fstat that the trash root fd matches the real .trash dir."""
        trash_fd = os.open(arena["trash"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            real_trash_ino = os.fstat(trash_fd).st_ino
        finally:
            os.close(trash_fd)

        trash_name = _fd_anchored_phase1_rename(
            activated_repos_dir=arena["activated_repos_dir"],
            username="alice",
            user_alias="my-repo",
        )

        # Verify the renamed entry exists under the real trash (uses trash_name).
        assert os.path.exists(os.path.join(arena["trash"], trash_name))
        # The trash dir's inode must be what we opened — proves rename went to
        # the pinned fd, not a swapped path.
        assert os.stat(arena["trash"]).st_ino == real_trash_ino

    def test_creates_trash_dir_if_missing(self, tmp_path):
        """If .trash doesn't exist yet, helper must create it (makedirs via fd not possible
        for non-existing dirs, so we pre-create before opening — verify it works)."""
        activated_repos_dir = tmp_path / "activated_repos"
        user_dir = activated_repos_dir / "bob"
        repo_dir = user_dir / "proj"
        repo_dir.mkdir(parents=True)
        (repo_dir / "f.txt").write_text("x")
        # No .trash yet — helper must create it atomically
        trash_name = _fd_anchored_phase1_rename(
            activated_repos_dir=str(activated_repos_dir),
            username="bob",
            user_alias="proj",
        )
        assert (activated_repos_dir / ".trash" / trash_name).exists()


# ---------------------------------------------------------------------------
# BLOCKER #1: .trash symlink swap cannot redirect rename
# ---------------------------------------------------------------------------


class TestTrashSymlinkSwapResistance:
    """Even if .trash is a symlink, the rename must land in the real .trash inode.

    The helper must open .trash with O_NOFOLLOW so a symlink swap between
    makedirs and rename cannot redirect the repo to an attacker-controlled dir.
    """

    def test_symlinked_trash_dir_raises_value_error(self, tmp_path):
        """If .trash IS a symlink when we try to open it, helper must raise."""
        activated_repos_dir = tmp_path / "activated_repos"
        user_dir = activated_repos_dir / "alice"
        repo_dir = user_dir / "my-repo"
        repo_dir.mkdir(parents=True)

        # Attacker's dir (outside activated_repos)
        attacker_dir = tmp_path / "attacker"
        attacker_dir.mkdir()

        # Create .trash as a symlink to the attacker dir
        trash_link = activated_repos_dir / ".trash"
        os.symlink(str(attacker_dir), str(trash_link))

        with pytest.raises((ValueError, OSError)):
            _fd_anchored_phase1_rename(
                activated_repos_dir=str(activated_repos_dir),
                username="alice",
                user_alias="my-repo",
            )

        # Repo must NOT be in the attacker's dir
        assert not any(attacker_dir.iterdir()), "repo must NOT land in attacker dir"

    def test_repo_not_moved_when_trash_is_symlink(self, tmp_path):
        """Verify the user repo stays intact when .trash is a symlink (rename refused)."""
        activated_repos_dir = tmp_path / "activated_repos"
        user_dir = activated_repos_dir / "alice"
        repo_dir = user_dir / "my-repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "keep.txt").write_text("safe")

        attacker_dir = tmp_path / "attacker"
        attacker_dir.mkdir()
        trash_link = activated_repos_dir / ".trash"
        os.symlink(str(attacker_dir), str(trash_link))

        try:
            _fd_anchored_phase1_rename(
                activated_repos_dir=str(activated_repos_dir),
                username="alice",
                user_alias="my-repo",
            )
        except (ValueError, OSError):
            pass  # expected

        # Repo must remain untouched since rename was refused
        assert (repo_dir / "keep.txt").exists(), (
            "repo must be intact when rename refused"
        )


# ---------------------------------------------------------------------------
# BLOCKER #2: username ancestor symlink swap cannot redirect rename
# ---------------------------------------------------------------------------


class TestUsernameAncestorSymlinkSwapResistance:
    """If {username} dir is replaced by a symlink, rename must refuse."""

    def test_symlinked_username_dir_raises(self, tmp_path):
        """Alice's user dir is replaced by a symlink to an attacker-controlled dir."""
        activated_repos_dir = tmp_path / "activated_repos"
        trash_dir = activated_repos_dir / ".trash"
        trash_dir.mkdir(parents=True)

        # Attacker's substitute user dir
        attacker_user_dir = tmp_path / "attacker_user"
        attacker_repo = attacker_user_dir / "my-repo"
        attacker_repo.mkdir(parents=True)
        (attacker_repo / "stolen.txt").write_text("stolen")

        # Create alice as a symlink to attacker_user_dir
        alice_link = activated_repos_dir / "alice"
        os.symlink(str(attacker_user_dir), str(alice_link))

        with pytest.raises((ValueError, OSError)):
            _fd_anchored_phase1_rename(
                activated_repos_dir=str(activated_repos_dir),
                username="alice",
                user_alias="my-repo",
            )

    def test_repo_not_stolen_when_username_is_symlink(self, tmp_path):
        """Attacker's repo must NOT be moved into .trash when username is a symlink."""
        activated_repos_dir = tmp_path / "activated_repos"
        trash_dir = activated_repos_dir / ".trash"
        trash_dir.mkdir(parents=True)

        attacker_user_dir = tmp_path / "attacker_user"
        attacker_repo = attacker_user_dir / "my-repo"
        attacker_repo.mkdir(parents=True)
        (attacker_repo / "stolen.txt").write_text("stolen")

        alice_link = activated_repos_dir / "alice"
        os.symlink(str(attacker_user_dir), str(alice_link))

        try:
            _fd_anchored_phase1_rename(
                activated_repos_dir=str(activated_repos_dir),
                username="alice",
                user_alias="my-repo",
            )
        except (ValueError, OSError):
            pass  # expected

        # Attacker's directory must NOT appear in trash
        trash_contents = list(trash_dir.iterdir())
        assert len(trash_contents) == 0, "attacker's repo must NOT land in trash"


# ---------------------------------------------------------------------------
# Symlinked activated_repos_dir itself is refused
# ---------------------------------------------------------------------------


class TestSymlinkedActivatedReposDirRefused:
    """If activated_repos_dir itself is a symlink, the helper must refuse."""

    def test_symlinked_activated_repos_dir_raises(self, tmp_path):
        real_dir = tmp_path / "real_activated_repos"
        user_dir = real_dir / "alice" / "my-repo"
        user_dir.mkdir(parents=True)

        link = tmp_path / "linked_activated_repos"
        os.symlink(str(real_dir), str(link))

        with pytest.raises((ValueError, OSError)):
            _fd_anchored_phase1_rename(
                activated_repos_dir=str(link),
                username="alice",
                user_alias="my-repo",
            )


# ---------------------------------------------------------------------------
# fd open flags spy: verify O_DIRECTORY|O_NOFOLLOW are used
# ---------------------------------------------------------------------------


class TestFdOpenFlagsUsed:
    """Verify the helper opens directories with O_DIRECTORY|O_NOFOLLOW."""

    def test_o_nofollow_used_for_activated_repos_dir(self, arena, monkeypatch):
        """At least one os.open call must use O_DIRECTORY|O_NOFOLLOW without dir_fd."""
        opened_without_dir_fd = []
        original_open = os.open

        def spy_open(path, flags, mode=0o777, *, dir_fd=None):
            if dir_fd is None:
                opened_without_dir_fd.append((path, flags))
            return (
                original_open(path, flags, mode, dir_fd=dir_fd)
                if dir_fd is not None
                else original_open(path, flags, mode)
            )

        monkeypatch.setattr(os, "open", spy_open)

        _fd_anchored_phase1_rename(
            activated_repos_dir=arena["activated_repos_dir"],
            username="alice",
            user_alias="my-repo",
        )

        # At least one top-level open (activated_repos_dir or .trash) must use NOFOLLOW
        nofollow_opens = [
            (p, f)
            for p, f in opened_without_dir_fd
            if (f & os.O_NOFOLLOW) and (f & os.O_DIRECTORY)
        ]
        assert nofollow_opens, (
            "Expected at least one os.open with O_DIRECTORY|O_NOFOLLOW for top-level dirs"
        )

    def test_dual_dir_fd_rename_called(self, arena, monkeypatch):
        """os.rename must be called with both src_dir_fd and dst_dir_fd set."""
        rename_calls = []
        original_rename = os.rename

        def spy_rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
            rename_calls.append((src, dst, src_dir_fd, dst_dir_fd))
            return original_rename(
                src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd
            )

        monkeypatch.setattr(os, "rename", spy_rename)

        _fd_anchored_phase1_rename(
            activated_repos_dir=arena["activated_repos_dir"],
            username="alice",
            user_alias="my-repo",
        )

        assert rename_calls, "os.rename must be called"
        src, dst, src_fd, dst_fd = rename_calls[0]
        assert src_fd is not None, "src_dir_fd must be set (fd-anchored)"
        assert dst_fd is not None, "dst_dir_fd must be set (fd-anchored)"
        # src and dst must be basenames only
        assert "/" not in src, "src must be a basename"
        assert "/" not in dst, "dst must be a basename"


# ---------------------------------------------------------------------------
# Nonexistent repo: helper must raise FileNotFoundError / OSError
# ---------------------------------------------------------------------------


class TestNonexistentRepo:
    def test_raises_when_repo_dir_missing(self, arena):
        """Renaming a nonexistent repo should raise (not silently succeed)."""
        with pytest.raises((OSError, FileNotFoundError, ValueError)):
            _fd_anchored_phase1_rename(
                activated_repos_dir=arena["activated_repos_dir"],
                username="alice",
                user_alias="nonexistent-repo",
            )
