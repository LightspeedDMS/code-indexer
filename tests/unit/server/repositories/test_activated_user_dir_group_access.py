"""Per-user activated-repos directory must be group-writable for the CoW daemon.

In cluster mode the clone destination ``activated-repos/<user>/<alias>`` is
created by the separate CoW storage daemon, which runs as a DIFFERENT OS user
than cidx-server. cidx-server creates ``activated-repos/<user>/`` itself (it
also writes ``<alias>_metadata.json`` there), so BOTH users need write+search
permission on that directory. The daemon reaches it through membership of
the configured service group (installer + auto-updater self-heal), which only
helps if the directory is actually group-writable.

A bare ``os.makedirs`` honours the process umask (systemd's default is 022),
producing 0755 -- the daemon then has no write access and a brand-new user
cannot activate any repository. These tests pin the directory to an explicit
0o2775 (owner rwx, group rwx + setgid, others r-x -- never world-writable)
independent of the umask, using real filesystem operations in a tmp dir.
"""

import os
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoCloneNotStartedError,
    ActivatedRepoError,
    ActivatedRepoManager,
)
from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
from code_indexer.server.repositories.user_dir_permissions import (
    USER_DIR_MODE,
    ensure_activated_user_dir,
)
from code_indexer.server.utils.config_manager import ServerResourceConfig

EXPECTED_USER_DIR_MODE = 0o2775


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@contextmanager
def _umask(value: int) -> Iterator[None]:
    previous = os.umask(value)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def golden_repo_manager_stub() -> MagicMock:
    stub = MagicMock()
    golden_repo = GoldenRepo(
        alias="test-repo",
        repo_url="https://example.com/example/test-repo.git",
        default_branch="main",
        clone_path="/path/to/golden/test-repo",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    repos = {"test-repo": golden_repo}
    stub.golden_repos = repos
    stub.get_golden_repo.side_effect = lambda alias: repos.get(alias)
    stub.get_actual_repo_path.return_value = "/path/to/golden/test-repo"
    stub.resource_config = ServerResourceConfig()
    return stub


@pytest.fixture
def manager(
    tmp_path: Path, golden_repo_manager_stub: MagicMock
) -> ActivatedRepoManager:
    return ActivatedRepoManager(
        data_dir=str(tmp_path / "data"),
        golden_repo_manager=golden_repo_manager_stub,
        background_job_manager=MagicMock(),
        clone_backend=MagicMock(),
        index_manager=None,
    )


class TestActivationUserDirMode:
    def test_activation_creates_user_dir_group_writable_setgid_before_clone(
        self, manager: ActivatedRepoManager
    ) -> None:
        """The user dir must already be 0o2775 at the moment the clone is
        requested -- that is when the CoW daemon needs to write into it."""
        observed_modes: List[int] = []

        def _record_parent_mode_and_stop(source_path, dest_path, **kwargs):
            observed_modes.append(_mode(os.path.dirname(dest_path)))
            # Stop activation here without triggering the orphan-cleanup
            # grace loop (Bug #1618 path re-raises immediately).
            raise ActivatedRepoCloneNotStartedError("stop after observing mode")

        with (
            _umask(0o022),
            patch.object(
                manager,
                "_clone_with_copy_on_write",
                side_effect=_record_parent_mode_and_stop,
            ),
        ):
            with pytest.raises(ActivatedRepoError):
                manager._do_activate_repository(
                    username="newuser",
                    golden_repo_alias="test-repo",
                    branch_name="main",
                    user_alias="my-repo",
                )

        assert observed_modes == [EXPECTED_USER_DIR_MODE], (
            f"user dir mode at clone time was "
            f"{[oct(m) for m in observed_modes]}, expected "
            f"{oct(EXPECTED_USER_DIR_MODE)}"
        )

    def test_metadata_save_creates_user_dir_with_group_access(
        self, manager: ActivatedRepoManager
    ) -> None:
        with _umask(0o022):
            manager._save_metadata_file("otheruser", "some-repo", {"k": "v"})
        user_dir = os.path.join(manager.activated_repos_dir, "otheruser")
        assert _mode(user_dir) == EXPECTED_USER_DIR_MODE


def _supplementary_gid() -> int:
    """A gid this process belongs to that is NOT its primary gid."""
    others = [g for g in os.getgroups() if g != os.getgid()]
    if not others:
        pytest.skip("test process has no supplementary group")
    return others[0]


class TestUserDirGroup:
    """The user dir must carry the service's primary group (the group the CoW
    daemon is joined to), never a group inherited from a setgid parent."""

    def test_new_dir_under_setgid_parent_gets_process_primary_gid(
        self, tmp_path: Path
    ) -> None:
        parent = tmp_path / "activated-repos"
        parent.mkdir()
        os.chown(parent, -1, _supplementary_gid())
        os.chmod(parent, 0o2775)
        user_dir = str(parent / "frank")

        ensure_activated_user_dir(user_dir)

        assert os.stat(user_dir).st_gid == os.getgid()
        assert _mode(user_dir) == EXPECTED_USER_DIR_MODE

    def test_repairs_gid_of_owned_existing_dir(self, tmp_path: Path) -> None:
        user_dir = tmp_path / "grace"
        user_dir.mkdir()
        os.chown(user_dir, -1, _supplementary_gid())
        os.chmod(user_dir, 0o2775)

        ensure_activated_user_dir(str(user_dir))

        assert os.stat(user_dir).st_gid == os.getgid()
        assert _mode(str(user_dir)) == EXPECTED_USER_DIR_MODE


class TestEnsureActivatedUserDir:
    @pytest.mark.parametrize("umask_value", [0o000, 0o022, 0o077])
    def test_creates_missing_dir_with_exact_mode_regardless_of_umask(
        self, tmp_path: Path, umask_value: int
    ) -> None:
        user_dir = str(tmp_path / "activated-repos" / "alice")
        with _umask(umask_value):
            ensure_activated_user_dir(user_dir)
        assert _mode(user_dir) == USER_DIR_MODE == EXPECTED_USER_DIR_MODE
        assert not _mode(user_dir) & stat.S_IWOTH, "must never be world-writable"
        assert os.stat(user_dir).st_gid == os.getgid()

    def test_repairs_owned_dir_created_under_umask_022(self, tmp_path: Path) -> None:
        """Directories created by an older server (0755) converge on the
        next call: group rwx + setgid added, other bits preserved."""
        user_dir = tmp_path / "bob"
        user_dir.mkdir()
        os.chmod(user_dir, 0o755)

        ensure_activated_user_dir(str(user_dir))

        assert _mode(str(user_dir)) == 0o2775

    def test_repair_preserves_restrictive_other_bits(self, tmp_path: Path) -> None:
        user_dir = tmp_path / "carol"
        user_dir.mkdir()
        os.chmod(user_dir, 0o700)

        ensure_activated_user_dir(str(user_dir))

        assert _mode(str(user_dir)) == 0o2770

    def test_correct_dir_is_left_untouched(self, tmp_path: Path) -> None:
        user_dir = tmp_path / "dave"
        user_dir.mkdir()
        os.chmod(user_dir, 0o2775)
        (user_dir / "keep.json").write_text("{}")
        ctime_before = os.stat(user_dir).st_ctime_ns

        ensure_activated_user_dir(str(user_dir))
        ensure_activated_user_dir(str(user_dir))

        assert os.stat(user_dir).st_ctime_ns == ctime_before, "no chmod expected"
        assert (user_dir / "keep.json").read_text() == "{}"

    @pytest.mark.skipif(os.geteuid() == 0, reason="needs a dir owned by another uid")
    def test_dir_owned_by_another_user_is_not_touched(self) -> None:
        """A root-owned directory stands in for one provisioned by another
        user: it must be accepted as-is, never chmod-ed (which would fail)."""
        foreign_dir = "/"
        mode_before = _mode(foreign_dir)

        ensure_activated_user_dir(foreign_dir)

        assert _mode(foreign_dir) == mode_before

    def test_existing_non_directory_raises(self, tmp_path: Path) -> None:
        not_a_dir = tmp_path / "erin"
        not_a_dir.write_text("oops")

        with pytest.raises(NotADirectoryError):
            ensure_activated_user_dir(str(not_a_dir))
