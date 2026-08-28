"""
Tests closing the second, previously-missed call site for Issue #1511:
CoW-daemon snapshot/clone creation must defensively ensure the source tree
is group+other readable/traversable BEFORE handing it off to a
remote-executing clone backend (the CoW daemon), since the daemon may run as
a different OS user (on a different host) than the process that wrote the
golden-repo's index files.

Issue #1511's original fix only wired the preflight into
VersionedSnapshotManager._create_clone_backend_snapshot (used by
refresh/snapshot creation). Repository ACTIVATION calls
ActivatedRepoManager._clone_with_copy_on_write, which invokes
self._clone_backend.create_clone_at_path(...) directly -- never through
VersionedSnapshotManager -- so the preflight never ran for activation,
reproducing the exact "Permission denied" failure live on staging.

Mirrors the structure of
tests/unit/server/storage/shared/test_snapshot_manager_1511.py and the
dependency-injection fixture pattern of
tests/unit/server/repositories/test_activated_repo_manager_cancel_1342.py.
"""

import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
from code_indexer.server.utils.config_manager import ServerResourceConfig


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class CowDaemonBackend:
    """Test double whose class name matches the production gating check
    (``type(self._clone_backend).__name__ == "CowDaemonBackend"``).

    Records the mode bits of every entry under ``source_path`` AT CALL TIME
    so tests can prove the preflight ran BEFORE create_clone_at_path was
    invoked -- not merely that modes happen to be correct afterward.
    """

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []
        self.modes_at_call_time: List[Dict[Path, int]] = []

    def create_clone_at_path(
        self,
        source_path: str,
        dest_path: str,
        preserve_attrs: bool = True,
        timeout: float = 3600,
        cancel_check: Any = None,
    ) -> str:
        self.calls.append((source_path, dest_path))
        snapshot: Dict[Path, int] = {}
        src = Path(source_path)
        if src.exists():
            snapshot[src] = _mode(src)
            for entry in src.rglob("*"):
                snapshot[entry] = _mode(entry)
        self.modes_at_call_time.append(snapshot)
        return dest_path


class LocalCloneBackend:
    """Test double for the non-CowDaemon path -- must NOT trigger chmod."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []

    def create_clone_at_path(
        self,
        source_path: str,
        dest_path: str,
        preserve_attrs: bool = True,
        timeout: float = 3600,
        cancel_check: Any = None,
    ) -> str:
        self.calls.append((source_path, dest_path))
        return dest_path


def _build_source_tree(tmp_path: Path) -> Path:
    """Build a small file/dir tree with restrictive permissions, simulating
    golden-repo index files written by the code-indexer OS user with a
    restrictive umask (mode 600 files / 700 dirs -- the exact staging
    reproduction from Issue #1511)."""
    source_dir = tmp_path / "source_repo"
    sub_dir = source_dir / ".code-indexer" / "index" / "voyage-code-3"
    sub_dir.mkdir(parents=True)

    file1 = source_dir / "collection_meta.json"
    file1.write_text("{}")
    file2 = sub_dir / "hnsw_index.bin"
    file2.write_text("binary-data")

    os.chmod(file1, 0o600)
    os.chmod(file2, 0o600)
    os.chmod(sub_dir, 0o700)
    os.chmod(source_dir, 0o700)

    return source_dir


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def golden_repo_manager_mock():
    from unittest.mock import MagicMock

    mock = MagicMock()
    golden_repo = GoldenRepo(
        alias="test-repo",
        repo_url="https://github.com/example/test-repo.git",
        default_branch="main",
        clone_path="/path/to/golden/test-repo",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    golden_repos_dict = {"test-repo": golden_repo}
    mock.golden_repos = golden_repos_dict
    mock.get_golden_repo.side_effect = lambda alias: golden_repos_dict.get(alias)
    mock.get_actual_repo_path.return_value = "/path/to/golden/test-repo"
    mock.resource_config = ServerResourceConfig()
    return mock


@pytest.fixture
def background_job_manager_mock():
    from unittest.mock import MagicMock

    mock = MagicMock()
    mock.submit_job.return_value = "job-123"
    return mock


class TestActivationCowDaemonPermissionPreflight:
    def test_chmod_applied_before_create_clone_for_cow_daemon_backend(
        self,
        temp_data_dir,
        golden_repo_manager_mock,
        background_job_manager_mock,
        tmp_path,
    ) -> None:
        """RED: activation's _clone_with_copy_on_write does not yet call the
        #1511 preflight helper, so modes captured AT create_clone_at_path
        call time remain restrictive."""
        source_dir = _build_source_tree(tmp_path)
        dest_path = tmp_path / "dest"
        dest_path.mkdir()

        backend = CowDaemonBackend()
        manager = ActivatedRepoManager(
            data_dir=temp_data_dir,
            golden_repo_manager=golden_repo_manager_mock,
            background_job_manager=background_job_manager_mock,
            clone_backend=backend,
            index_manager=None,
        )

        manager._clone_with_copy_on_write(str(source_dir), str(dest_path))

        assert backend.calls, "create_clone_at_path should have been invoked"
        assert len(backend.modes_at_call_time) == 1
        modes_at_call = backend.modes_at_call_time[0]
        assert modes_at_call, "expected non-empty snapshot of source tree"

        for path, mode in modes_at_call.items():
            if path.is_dir():
                assert mode & 0o055 == 0o055, f"dir {path} mode {oct(mode)}"
            else:
                assert mode & 0o044 == 0o044, f"file {path} mode {oct(mode)}"

    def test_no_chmod_for_non_cow_daemon_backend(
        self,
        temp_data_dir,
        golden_repo_manager_mock,
        background_job_manager_mock,
        tmp_path,
    ) -> None:
        """Gating must be name-based: a non-CowDaemonBackend clone_backend
        must NOT have its source tree permissions touched."""
        source_dir = _build_source_tree(tmp_path)
        dest_path = tmp_path / "dest"
        dest_path.mkdir()

        backend = LocalCloneBackend()
        manager = ActivatedRepoManager(
            data_dir=temp_data_dir,
            golden_repo_manager=golden_repo_manager_mock,
            background_job_manager=background_job_manager_mock,
            clone_backend=backend,
            index_manager=None,
        )

        original_modes = {p: _mode(p) for p in source_dir.rglob("*")}
        original_modes[source_dir] = _mode(source_dir)

        manager._clone_with_copy_on_write(str(source_dir), str(dest_path))

        assert backend.calls, "create_clone_at_path should have been invoked"
        for path, orig_mode in original_modes.items():
            assert _mode(path) == orig_mode, f"{path} mode changed unexpectedly"
