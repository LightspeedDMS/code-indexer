"""
Unit tests for VersionedSnapshotManager.

OntapFlexCloneClient and subprocess.run are mocked because:
- No real ONTAP cluster is available in unit tests.
- We do not want to actually copy gigabytes of data during unit tests.

All filesystem CoW tests that need actual path creation use a tmp_path fixture.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.storage.shared.snapshot_manager import VersionedSnapshotManager
from code_indexer.server.storage.shared.ontap_flexclone_client import (
    OntapFlexCloneClient,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_flexclone_client() -> MagicMock:
    """Return a MagicMock that looks like an OntapFlexCloneClient."""
    client = MagicMock(spec=OntapFlexCloneClient)
    client.create_clone.return_value = {
        "uuid": "test-uuid",
        "name": "cidx_clone_myrepo_1700000000",
        "job_uuid": "job-abc",
    }
    client.delete_clone.return_value = True
    return client


# ---------------------------------------------------------------------------
# uses_flexclone property
# ---------------------------------------------------------------------------


def test_uses_flexclone_true_when_client_provided() -> None:
    """uses_flexclone is True when an OntapFlexCloneClient is supplied."""
    client = _make_flexclone_client()
    manager = VersionedSnapshotManager(flexclone_client=client)
    assert manager.uses_flexclone is True


def test_uses_flexclone_false_when_no_client() -> None:
    """uses_flexclone is False when no OntapFlexCloneClient is supplied."""
    manager = VersionedSnapshotManager()
    assert manager.uses_flexclone is False


# ---------------------------------------------------------------------------
# create_snapshot — FlexClone mode
# ---------------------------------------------------------------------------


def test_create_snapshot_uses_flexclone_when_client_available() -> None:
    """create_snapshot calls OntapFlexCloneClient.create_clone in FlexClone mode."""
    flexclone = _make_flexclone_client()
    manager = VersionedSnapshotManager(
        flexclone_client=flexclone,
        mount_point="/mnt/fsx",
    )

    with patch("code_indexer.server.storage.shared.snapshot_manager.time") as mock_time:
        mock_time.time.return_value = 1700000000
        result = manager.create_snapshot("myrepo", source_path="/golden-repos/myrepo")

    flexclone.create_clone.assert_called_once()
    call_args = flexclone.create_clone.call_args
    clone_name = call_args[0][0]
    assert clone_name == "cidx_clone_myrepo_1700000000"
    assert call_args[1]["junction_path"] == "/cidx_clone_myrepo_1700000000"
    assert result == "/mnt/fsx/cidx_clone_myrepo_1700000000"


def test_create_snapshot_flexclone_returns_mount_path() -> None:
    """create_snapshot returns the mount_point/clone_name path in FlexClone mode."""
    flexclone = _make_flexclone_client()
    manager = VersionedSnapshotManager(
        flexclone_client=flexclone,
        mount_point="/mnt/fsx",
    )

    with patch("code_indexer.server.storage.shared.snapshot_manager.time") as mock_time:
        mock_time.time.return_value = 1700000042
        result = manager.create_snapshot("repo-x", source_path="/ignored")

    assert result == "/mnt/fsx/cidx_clone_repo-x_1700000042"


def test_create_snapshot_flexclone_does_not_call_subprocess() -> None:
    """In FlexClone mode, subprocess.run is never called."""
    flexclone = _make_flexclone_client()
    manager = VersionedSnapshotManager(flexclone_client=flexclone)

    with (
        patch("code_indexer.server.storage.shared.snapshot_manager.time") as mock_time,
        patch("subprocess.run") as mock_run,
    ):
        mock_time.time.return_value = 1700000000
        manager.create_snapshot("myrepo", source_path="/src")

    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# create_snapshot — CoW (filesystem) mode
# ---------------------------------------------------------------------------


def test_create_snapshot_cow_calls_cp_reflink(tmp_path: Path) -> None:
    """create_snapshot runs cp --reflink=auto in CoW mode."""
    manager = VersionedSnapshotManager(
        flexclone_client=None,
        versioned_base=str(tmp_path),
    )

    with (
        patch("code_indexer.server.storage.shared.snapshot_manager.time") as mock_time,
        patch("subprocess.run") as mock_run,
    ):
        mock_time.time.return_value = 1700000000
        result = manager.create_snapshot("myrepo", source_path="/golden-repos/myrepo")

    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "cp"
    assert "--reflink=auto" in cmd
    assert "-a" in cmd
    assert "/golden-repos/myrepo" in cmd
    expected_dest = str(tmp_path / ".versioned" / "myrepo" / "v_1700000000")
    assert expected_dest in cmd
    assert result == expected_dest


def test_create_snapshot_cow_creates_versioned_directory(tmp_path: Path) -> None:
    """create_snapshot creates the .versioned/{alias} parent directory."""
    manager = VersionedSnapshotManager(
        flexclone_client=None,
        versioned_base=str(tmp_path),
    )

    with (
        patch("code_indexer.server.storage.shared.snapshot_manager.time") as mock_time,
        patch("subprocess.run"),
    ):
        mock_time.time.return_value = 1700000001
        manager.create_snapshot("newrepo", source_path="/src")

    expected_parent = tmp_path / ".versioned" / "newrepo"
    assert expected_parent.exists()


def test_create_snapshot_cow_does_not_call_flexclone() -> None:
    """In CoW mode, OntapFlexCloneClient methods are never called."""
    flexclone = _make_flexclone_client()
    manager = VersionedSnapshotManager(
        flexclone_client=None,  # explicitly no FlexClone
        versioned_base="/tmp/test",
    )

    with (
        patch("code_indexer.server.storage.shared.snapshot_manager.time") as mock_time,
        patch("subprocess.run"),
    ):
        mock_time.time.return_value = 1700000000
        manager.create_snapshot("repo", source_path="/src")

    flexclone.create_clone.assert_not_called()


def test_create_snapshot_cow_propagates_subprocess_error(tmp_path: Path) -> None:
    """create_snapshot propagates CalledProcessError from cp."""
    manager = VersionedSnapshotManager(
        flexclone_client=None,
        versioned_base=str(tmp_path),
    )

    with (
        patch("code_indexer.server.storage.shared.snapshot_manager.time") as mock_time,
        patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "cp")),
    ):
        mock_time.time.return_value = 1700000000
        with pytest.raises(subprocess.CalledProcessError):
            manager.create_snapshot("repo", source_path="/src")


def test_create_snapshot_cow_propagates_timeout(tmp_path: Path) -> None:
    """create_snapshot propagates TimeoutExpired from cp."""
    manager = VersionedSnapshotManager(
        flexclone_client=None,
        versioned_base=str(tmp_path),
        cow_timeout=30,
    )

    with (
        patch("code_indexer.server.storage.shared.snapshot_manager.time") as mock_time,
        patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cp", 30)),
    ):
        mock_time.time.return_value = 1700000000
        with pytest.raises(subprocess.TimeoutExpired):
            manager.create_snapshot("repo", source_path="/src")


# ---------------------------------------------------------------------------
# delete_snapshot — FlexClone mode
# ---------------------------------------------------------------------------


def test_delete_snapshot_delegates_to_flexclone_client() -> None:
    """delete_snapshot calls OntapFlexCloneClient.delete_clone in FlexClone mode."""
    flexclone = _make_flexclone_client()
    manager = VersionedSnapshotManager(
        flexclone_client=flexclone,
        mount_point="/mnt/fsx",
    )

    result = manager.delete_snapshot("myrepo", "/mnt/fsx/cidx_clone_myrepo_1700000000")

    assert result is True
    flexclone.delete_clone.assert_called_once_with("cidx_clone_myrepo_1700000000")


def test_delete_snapshot_flexclone_extracts_clone_name_from_path() -> None:
    """delete_snapshot derives the clone name from the version_path basename."""
    flexclone = _make_flexclone_client()
    manager = VersionedSnapshotManager(flexclone_client=flexclone)

    manager.delete_snapshot("any", "/mnt/fsx/cidx_clone_repo_x_9876543210")

    flexclone.delete_clone.assert_called_once_with("cidx_clone_repo_x_9876543210")


# ---------------------------------------------------------------------------
# delete_snapshot — CoW mode
# ---------------------------------------------------------------------------


def test_delete_snapshot_cow_removes_directory(tmp_path: Path) -> None:
    """delete_snapshot removes the CoW snapshot directory tree."""
    snapshot_dir = tmp_path / ".versioned" / "myrepo" / "v_1700000000"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "somefile.txt").write_text("content")

    manager = VersionedSnapshotManager(flexclone_client=None)

    result = manager.delete_snapshot("myrepo", str(snapshot_dir))

    assert result is True
    assert not snapshot_dir.exists()


def test_delete_snapshot_cow_idempotent_when_directory_missing() -> None:
    """delete_snapshot returns True even when the directory is already gone."""
    manager = VersionedSnapshotManager(flexclone_client=None)

    result = manager.delete_snapshot("myrepo", "/nonexistent/path/v_1700000000")

    assert result is True


def test_delete_snapshot_cow_does_not_call_flexclone() -> None:
    """In CoW mode, delete_snapshot never calls OntapFlexCloneClient."""
    flexclone = _make_flexclone_client()
    manager = VersionedSnapshotManager(flexclone_client=None)

    manager.delete_snapshot("repo", "/nonexistent/v_0")

    flexclone.delete_clone.assert_not_called()


# ---------------------------------------------------------------------------
# get_snapshot_path
# ---------------------------------------------------------------------------


def test_get_snapshot_path_flexclone_mode() -> None:
    """get_snapshot_path returns mount_point/cidx_clone_{alias}_{ts} in FlexClone mode."""
    flexclone = _make_flexclone_client()
    manager = VersionedSnapshotManager(
        flexclone_client=flexclone,
        mount_point="/mnt/fsx",
    )

    path = manager.get_snapshot_path("myrepo", "1700000000")

    assert path == "/mnt/fsx/cidx_clone_myrepo_1700000000"


def test_get_snapshot_path_cow_mode() -> None:
    """get_snapshot_path returns .versioned/{alias}/v_{ts} in CoW mode."""
    manager = VersionedSnapshotManager(
        flexclone_client=None,
        versioned_base="/golden-repos",
    )

    path = manager.get_snapshot_path("myrepo", "1700000000")

    assert path == "/golden-repos/.versioned/myrepo/v_1700000000"


def test_get_snapshot_path_cow_mode_no_double_slash() -> None:
    """versioned_base trailing slash does not double up in the path."""
    manager = VersionedSnapshotManager(
        flexclone_client=None,
        versioned_base="/golden-repos",
    )

    path = manager.get_snapshot_path("repo-b", "1234567890")

    assert "//" not in path
    assert path == "/golden-repos/.versioned/repo-b/v_1234567890"


# ---------------------------------------------------------------------------
# clone_backend integration (Story #510 AC7)
# ---------------------------------------------------------------------------


def _make_mock_clone_backend() -> MagicMock:
    """Return a MagicMock that satisfies the CloneBackend protocol."""
    backend = MagicMock()
    backend.create_clone.return_value = "/mnt/cow/myrepo/v_1700000000"
    backend.delete_clone.return_value = True
    return backend


def test_create_snapshot_delegates_to_clone_backend_when_provided() -> None:
    """create_snapshot calls clone_backend.create_clone when clone_backend is set."""
    backend = _make_mock_clone_backend()
    manager = VersionedSnapshotManager(clone_backend=backend)

    with patch("code_indexer.server.storage.shared.snapshot_manager.time") as mock_time:
        mock_time.time.return_value = 1700000000
        result = manager.create_snapshot("myrepo", source_path="/golden-repos/myrepo")

    backend.create_clone.assert_called_once_with(
        "/golden-repos/myrepo", "myrepo", "v_1700000000"
    )
    assert result == "/mnt/cow/myrepo/v_1700000000"


def test_create_snapshot_clone_backend_does_not_call_flexclone() -> None:
    """When clone_backend is set, flexclone_client is never called."""
    flexclone = _make_flexclone_client()
    backend = _make_mock_clone_backend()
    manager = VersionedSnapshotManager(
        flexclone_client=flexclone, clone_backend=backend
    )

    with patch("code_indexer.server.storage.shared.snapshot_manager.time") as mock_time:
        mock_time.time.return_value = 1700000000
        manager.create_snapshot("myrepo", source_path="/src")

    flexclone.create_clone.assert_not_called()
    backend.create_clone.assert_called_once()


def test_create_snapshot_clone_backend_does_not_call_subprocess() -> None:
    """When clone_backend is set, subprocess.run is never called."""
    backend = _make_mock_clone_backend()
    manager = VersionedSnapshotManager(
        clone_backend=backend, versioned_base="/tmp/test"
    )

    with (
        patch("code_indexer.server.storage.shared.snapshot_manager.time") as mock_time,
        patch("subprocess.run") as mock_run,
    ):
        mock_time.time.return_value = 1700000000
        manager.create_snapshot("myrepo", source_path="/src")

    mock_run.assert_not_called()


def test_delete_snapshot_delegates_to_clone_backend_when_provided() -> None:
    """delete_snapshot calls clone_backend.delete_clone when clone_backend is set."""
    backend = _make_mock_clone_backend()
    manager = VersionedSnapshotManager(clone_backend=backend)

    result = manager.delete_snapshot("myrepo", "/mnt/cow/myrepo/v_1700000000")

    backend.delete_clone.assert_called_once_with("/mnt/cow/myrepo/v_1700000000")
    assert result is True


def test_delete_snapshot_clone_backend_does_not_call_flexclone() -> None:
    """When clone_backend is set, flexclone_client.delete_clone is never called."""
    flexclone = _make_flexclone_client()
    backend = _make_mock_clone_backend()
    manager = VersionedSnapshotManager(
        flexclone_client=flexclone, clone_backend=backend
    )

    manager.delete_snapshot("myrepo", "/mnt/cow/myrepo/v_1700000000")

    flexclone.delete_clone.assert_not_called()
    backend.delete_clone.assert_called_once()


def test_none_clone_backend_preserves_flexclone_create_behavior() -> None:
    """When clone_backend is None, flexclone_client is used (existing behavior)."""
    flexclone = _make_flexclone_client()
    manager = VersionedSnapshotManager(
        flexclone_client=flexclone,
        clone_backend=None,
        mount_point="/mnt/fsx",
    )

    with patch("code_indexer.server.storage.shared.snapshot_manager.time") as mock_time:
        mock_time.time.return_value = 1700000000
        result = manager.create_snapshot("myrepo", source_path="/src")

    flexclone.create_clone.assert_called_once()
    assert result == "/mnt/fsx/cidx_clone_myrepo_1700000000"


def test_none_clone_backend_preserves_cow_create_behavior(tmp_path: "Path") -> None:
    """When clone_backend is None, CoW subprocess path is used (existing behavior)."""
    manager = VersionedSnapshotManager(
        flexclone_client=None,
        clone_backend=None,
        versioned_base=str(tmp_path),
    )

    with (
        patch("code_indexer.server.storage.shared.snapshot_manager.time") as mock_time,
        patch("subprocess.run") as mock_run,
    ):
        mock_time.time.return_value = 1700000000
        manager.create_snapshot("myrepo", source_path="/src")

    mock_run.assert_called_once()


def test_none_clone_backend_preserves_flexclone_delete_behavior() -> None:
    """When clone_backend is None, flexclone_client.delete_clone is used (existing behavior)."""
    flexclone = _make_flexclone_client()
    manager = VersionedSnapshotManager(
        flexclone_client=flexclone,
        clone_backend=None,
    )

    result = manager.delete_snapshot("myrepo", "/mnt/fsx/cidx_clone_myrepo_1700000000")

    flexclone.delete_clone.assert_called_once_with("cidx_clone_myrepo_1700000000")
    assert result is True


def test_none_clone_backend_preserves_cow_delete_behavior(tmp_path: "Path") -> None:
    """When clone_backend is None, CoW directory removal is used (existing behavior)."""
    snapshot_dir = tmp_path / ".versioned" / "myrepo" / "v_1700000000"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "file.txt").write_text("content")

    manager = VersionedSnapshotManager(flexclone_client=None, clone_backend=None)

    result = manager.delete_snapshot("myrepo", str(snapshot_dir))

    assert result is True
    assert not snapshot_dir.exists()
