"""Bug #1084: refresh_scheduler defense-in-depth NFS read-after-create barrier.

Even though the cow-daemon / ONTAP clone backends now wait for NFS visibility
at the create boundary, ``RefreshScheduler._create_snapshot`` ALSO confirms the
freshly returned ``versioned_path`` is visible BEFORE it runs the
``git restore`` / ``cidx fix-config`` subprocess steps (which use
``cwd=versioned_path`` and ENOENT on a not-yet-propagated path). This covers a
future non-cow backend or a very slow NFS, and is idempotent/fast when the path
is already visible.

These tests mirror the existing _create_snapshot fixtures (no real NFS).
"""

import shutil
import time
from pathlib import Path
from typing import List, Tuple
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler


@pytest.fixture
def golden_repos_dir(tmp_path):
    grd = tmp_path / "golden_repos"
    grd.mkdir(parents=True)
    return grd


@pytest.fixture
def config_mgr(tmp_path):
    return ConfigManager(tmp_path / ".code-indexer" / "config.json")


@pytest.fixture
def query_tracker():
    return QueryTracker()


@pytest.fixture
def cleanup_manager(query_tracker):
    return CleanupManager(query_tracker)


@pytest.fixture
def registry(golden_repos_dir):
    return GlobalRegistry(str(golden_repos_dir))


@pytest.fixture
def mock_snapshot_manager(golden_repos_dir):
    mgr = MagicMock()

    def _create_snapshot(repo_name, source_path):
        versioned_path = (
            golden_repos_dir / ".versioned" / repo_name / f"v_{int(time.time())}"
        )
        versioned_path.mkdir(parents=True, exist_ok=True)
        for item in Path(source_path).iterdir():
            dest = versioned_path / item.name
            if item.is_dir():
                shutil.copytree(str(item), str(dest))
            else:
                shutil.copy2(str(item), str(dest))
        (versioned_path / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)
        return str(versioned_path)

    mgr.create_snapshot.side_effect = _create_snapshot
    return mgr


@pytest.fixture
def scheduler(
    golden_repos_dir,
    config_mgr,
    query_tracker,
    cleanup_manager,
    registry,
    mock_snapshot_manager,
):
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        registry=registry,
        snapshot_manager=mock_snapshot_manager,
    )


@pytest.fixture
def source_repo(tmp_path):
    src = tmp_path / "source_repo"
    src.mkdir()
    (src / "README.md").write_text("# Test Repo")
    (src / "main.py").write_text("def main(): pass")
    (src / ".git").mkdir()
    return src


class TestCreateSnapshotNfsVisibilityBarrier:
    def test_waits_for_visibility_before_subprocess_steps(self, scheduler, source_repo):
        """_create_snapshot must call the NFS visibility barrier on versioned_path
        BEFORE any subprocess.run (git restore / cidx fix-config)."""
        call_order: List[Tuple[str, object]] = []

        def record_wait(path, *args, **kwargs):
            call_order.append(("wait", str(path)))

        def record_subprocess(cmd, *args, **kwargs):
            call_order.append(("subprocess", list(cmd)))
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result

        with patch(
            "code_indexer.global_repos.refresh_scheduler.wait_for_nfs_visibility",
            side_effect=record_wait,
        ):
            with patch(
                "code_indexer.global_repos.refresh_scheduler.subprocess.run",
                side_effect=record_subprocess,
            ):
                result_path = scheduler._create_snapshot(
                    "myrepo-global", str(source_repo)
                )

        # The visibility wait must have happened.
        wait_calls = [c for c in call_order if c[0] == "wait"]
        assert wait_calls, "wait_for_nfs_visibility was never called on versioned_path"
        # It must reference the returned versioned_path.
        assert any(result_path == c[1] for c in wait_calls)

        # The FIRST subprocess step must come AFTER the first visibility wait.
        first_wait_idx = next(i for i, c in enumerate(call_order) if c[0] == "wait")
        subprocess_indices = [
            i for i, c in enumerate(call_order) if c[0] == "subprocess"
        ]
        if subprocess_indices:
            assert first_wait_idx < subprocess_indices[0], (
                "NFS visibility barrier must run BEFORE git restore / cidx "
                "fix-config subprocess steps"
            )

    def test_visibility_timeout_aborts_before_subprocess(self, scheduler, source_repo):
        """If the barrier raises (path never visible), no subprocess step runs and
        _create_snapshot fails loud (anti-fallback)."""
        subprocess_calls = []

        def record_subprocess(cmd, *args, **kwargs):
            subprocess_calls.append(list(cmd))
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result

        with patch(
            "code_indexer.global_repos.refresh_scheduler.wait_for_nfs_visibility",
            side_effect=RuntimeError("NFS visibility timeout"),
        ):
            with patch(
                "code_indexer.global_repos.refresh_scheduler.subprocess.run",
                side_effect=record_subprocess,
            ):
                with pytest.raises(RuntimeError):
                    scheduler._create_snapshot("myrepo-global", str(source_repo))

        assert subprocess_calls == [], (
            "No subprocess step should run when the visibility barrier fails"
        )
