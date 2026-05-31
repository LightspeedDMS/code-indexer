"""
Unit tests for Story #229: Index-Source-First Refresh Pipeline (core).

Tests verify that RefreshScheduler indexes the golden repo source BEFORE
performing the CoW clone, so the versioned snapshot inherits index data
via reflink and does NOT need re-indexing.

Acceptance criteria covered here:
- _index_source() and _create_snapshot() methods exist
- cidx index --fts runs on source BEFORE CoW clone
- cidx index --fts cwd is source_path in _index_source()
- cidx fix-config --force NOT called in _index_source()
- cidx fix-config --force IS called on versioned_path in _create_snapshot()
- _execute_refresh() calls _index_source() then _create_snapshot() in sequence
"""

import json
import shutil
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.config import ConfigManager


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


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
    """Mock snapshot_manager that replicates cp --reflink=auto via shutil.copytree."""
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
        # Simulate source was already indexed: index dir must exist in clone
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


# ---------------------------------------------------------------------------
# Tests: _index_source() and _create_snapshot() must exist
# ---------------------------------------------------------------------------


class TestIndexSourceAndCreateSnapshotExist:
    """Verify the two new methods exist and are callable."""

    def test_index_source_method_exists(self, scheduler):
        """_index_source() must exist on RefreshScheduler."""
        assert hasattr(scheduler, "_index_source"), (
            "RefreshScheduler must have a '_index_source()' method. "
            "Story #229: split _create_new_index() into _index_source() + _create_snapshot()."
        )
        assert callable(scheduler._index_source)

    def test_create_snapshot_method_exists(self, scheduler):
        """_create_snapshot() must exist on RefreshScheduler."""
        assert hasattr(scheduler, "_create_snapshot"), (
            "RefreshScheduler must have a '_create_snapshot()' method. "
            "Story #229: split _create_new_index() into _index_source() + _create_snapshot()."
        )
        assert callable(scheduler._create_snapshot)


# ---------------------------------------------------------------------------
# Tests: call order -- index source FIRST, then CoW clone
# ---------------------------------------------------------------------------


class TestCallOrder:
    """Verify cidx index runs on source before CoW clone happens."""

    def test_cidx_index_fts_runs_before_cow_clone(
        self, scheduler, registry, source_repo
    ):
        """
        AC: cidx index --fts runs on golden repo source path BEFORE CoW clone.

        Story #229 requires indexing on the source so the snapshot inherits
        the index via CoW reflink -- not the other way around.

        _index_source uses run_with_popen_progress for semantic indexing (Popen).
        We track call order by recording when popen_progress and cp are called.
        """
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "git@github.com:org/repo.git",
            str(source_repo),
        )

        call_sequence = []

        # Story #1034: CoW clone now goes through snapshot_manager.create_snapshot(),
        # not subprocess.run(["cp", "--reflink=auto", ...]). Wrap the existing
        # mock_snapshot_manager side_effect to also record into call_sequence.
        original_create_snapshot = (
            scheduler._snapshot_manager.create_snapshot.side_effect
        )

        def recording_create_snapshot(repo_name, source_path):
            call_sequence.append(("cow_clone", str(source_path)))
            return original_create_snapshot(repo_name, source_path)

        scheduler._snapshot_manager.create_snapshot.side_effect = (
            recording_create_snapshot
        )

        def mock_popen_progress(**kwargs):
            cwd = kwargs.get("cwd", "")
            call_sequence.append(("cidx_index_fts", str(cwd)))
            # Create index dir to allow _create_snapshot validation to pass
            (Path(cwd) / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)
            return 50

        def mock_run(cmd, **kwargs):
            cwd = kwargs.get("cwd", "")
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""

            if cmd[:2] == ["cidx", "fix-config"]:
                call_sequence.append(("fix_config", str(cwd)))

            return result

        with patch(
            "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
            return_value=(0, 0),
        ):
            with patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=mock_popen_progress,
            ):
                with patch("subprocess.run", side_effect=mock_run):
                    scheduler._index_source(
                        alias_name="test-repo-global", source_path=str(source_repo)
                    )
                    scheduler._create_snapshot(
                        alias_name="test-repo-global", source_path=str(source_repo)
                    )

        index_positions = [
            i for i, (name, _) in enumerate(call_sequence) if name == "cidx_index_fts"
        ]
        cow_positions = [
            i for i, (name, _) in enumerate(call_sequence) if name == "cow_clone"
        ]

        assert index_positions, "cidx index --fts was never called"
        assert cow_positions, (
            "CoW clone was never called: snapshot_manager.create_snapshot() was not invoked."
        )

        first_index = min(index_positions)
        first_cow = min(cow_positions)
        assert first_index < first_cow, (
            f"cidx index --fts (pos={first_index}) must run BEFORE CoW clone "
            f"(pos={first_cow}). Story #229 requires indexing source first."
        )

    def test_cidx_index_fts_cwd_is_source_path(self, scheduler, registry, source_repo):
        """
        AC: cidx index --fts must be invoked with cwd=source_path.

        run_with_popen_progress receives cwd=str(source_path) from _index_source.
        """
        registry.register_global_repo(
            "cwd-test-repo",
            "cwd-test-repo-global",
            "git@github.com:org/repo.git",
            str(source_repo),
        )

        index_cwds = []

        def mock_popen_progress(**kwargs):
            cwd = kwargs.get("cwd", "")
            index_cwds.append(str(cwd))
            # Create index dir so _create_snapshot validation passes if needed
            (Path(cwd) / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)
            return 50

        with patch(
            "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
            return_value=(0, 0),
        ):
            with patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=mock_popen_progress,
            ):
                with patch(
                    "subprocess.run",
                    return_value=MagicMock(returncode=0, stdout="", stderr=""),
                ):
                    scheduler._index_source(
                        alias_name="cwd-test-repo-global", source_path=str(source_repo)
                    )

        assert index_cwds, (
            "cidx index --fts (via run_with_popen_progress) was not called by _index_source()"
        )
        assert index_cwds[0] == str(source_repo), (
            f"cidx index --fts cwd must be source_path={source_repo}, got cwd={index_cwds[0]}."
        )


# ---------------------------------------------------------------------------
# Tests: fix-config -- clone only, never source
# ---------------------------------------------------------------------------


class TestFixConfigOnCloneOnly:
    """Verify cidx fix-config --force runs only on versioned_path, never source."""

    def test_fix_config_not_called_in_index_source(
        self, scheduler, registry, source_repo
    ):
        """
        C4: cidx fix-config --force must NOT be invoked inside _index_source().
        """
        registry.register_global_repo(
            "fix-config-test",
            "fix-config-test-global",
            "git@github.com:org/repo.git",
            str(source_repo),
        )

        fix_config_cwds = []

        def mock_run(cmd, **kwargs):
            cwd = kwargs.get("cwd", "")
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""

            if cmd[:2] == ["cidx", "fix-config"]:
                fix_config_cwds.append(str(cwd))

            return result

        def mock_popen_progress(**kwargs):
            cwd = kwargs.get("cwd", "")
            (Path(cwd) / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)
            return 50

        with patch(
            "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
            return_value=(0, 0),
        ):
            with patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=mock_popen_progress,
            ):
                with patch("subprocess.run", side_effect=mock_run):
                    scheduler._index_source(
                        alias_name="fix-config-test-global",
                        source_path=str(source_repo),
                    )

        assert fix_config_cwds == [], (
            f"C4: cidx fix-config was called {len(fix_config_cwds)} time(s) from _index_source(). "
            f"cwds: {fix_config_cwds}. fix-config MUST ONLY run in _create_snapshot() on the clone."
        )

    def test_fix_config_called_on_versioned_path_in_create_snapshot(
        self, scheduler, registry, source_repo
    ):
        """
        C4: cidx fix-config --force IS invoked with cwd=versioned_snapshot_path
        inside _create_snapshot().
        """
        registry.register_global_repo(
            "fix-config-clone-test",
            "fix-config-clone-test-global",
            "git@github.com:org/repo.git",
            str(source_repo),
        )

        fix_config_cwds = []

        def mock_run(cmd, **kwargs):
            cwd = kwargs.get("cwd", "")
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""

            if cmd[0] == "cp" and "--reflink=auto" in cmd:
                dst = cmd[-1]
                shutil.copytree(cmd[-2], dst)
                # Simulate source was already indexed: create index dir in clone
                (Path(dst) / ".code-indexer" / "index").mkdir(
                    parents=True, exist_ok=True
                )
            elif cmd[:2] == ["cidx", "fix-config"]:
                fix_config_cwds.append(str(cwd))

            return result

        with patch("subprocess.run", side_effect=mock_run):
            result_path = scheduler._create_snapshot(
                alias_name="fix-config-clone-test-global", source_path=str(source_repo)
            )

        assert fix_config_cwds, (
            "cidx fix-config was never called from _create_snapshot(). "
            "C4: fix-config MUST run on the versioned clone."
        )
        assert fix_config_cwds[0] != str(source_repo), (
            f"cidx fix-config cwd must NOT be source_path={source_repo}."
        )
        assert fix_config_cwds[0] == result_path, (
            f"cidx fix-config cwd={fix_config_cwds[0]} must equal returned versioned_path={result_path}."
        )


# ---------------------------------------------------------------------------
# Tests: _execute_refresh() call site
# ---------------------------------------------------------------------------


class TestExecuteRefreshCallSite:
    """
    C5: _execute_refresh() calls _index_source() then _create_snapshot() in sequence.
    """

    def test_execute_refresh_calls_index_source_then_create_snapshot(
        self, scheduler, registry, golden_repos_dir, source_repo
    ):
        """
        AC: _execute_refresh() calls _index_source() before _create_snapshot().
        """
        alias_name = "exec-refresh-test-global"
        registry.register_global_repo(
            "exec-refresh-test",
            alias_name,
            "git@github.com:org/repo.git",
            str(source_repo),
        )

        aliases_dir = golden_repos_dir / "aliases"
        aliases_dir.mkdir(exist_ok=True)
        (aliases_dir / f"{alias_name}.json").write_text(
            json.dumps({"target_path": str(source_repo)})
        )

        call_order = []
        fake_snapshot_path = str(
            golden_repos_dir / ".versioned" / "exec-refresh-test" / "v_1234567890"
        )

        def mock_index_source(
            alias_name, source_path, progress_callback=None, force_reconcile=False
        ):
            call_order.append("_index_source")

        def mock_create_snapshot(alias_name, source_path):
            call_order.append("_create_snapshot")
            Path(fake_snapshot_path).mkdir(parents=True, exist_ok=True)
            return fake_snapshot_path

        with patch.object(scheduler, "_index_source", side_effect=mock_index_source):
            with patch.object(
                scheduler, "_create_snapshot", side_effect=mock_create_snapshot
            ):
                with patch.object(scheduler.alias_manager, "swap_alias"):
                    with patch.object(scheduler.cleanup_manager, "schedule_cleanup"):
                        with patch.object(
                            scheduler.registry, "update_refresh_timestamp"
                        ):
                            with patch.object(
                                scheduler, "_detect_existing_indexes", return_value={}
                            ):
                                with patch.object(
                                    scheduler, "_reconcile_registry_with_filesystem"
                                ):
                                    with patch(
                                        "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
                                    ) as mock_gpu:
                                        mock_updater = MagicMock()
                                        mock_updater.has_changes.return_value = True
                                        mock_updater.get_source_path.return_value = str(
                                            source_repo
                                        )
                                        mock_gpu.return_value = mock_updater

                                        scheduler._execute_refresh(alias_name)

        assert "_index_source" in call_order, (
            "C5: _execute_refresh() must call _index_source()."
        )
        assert "_create_snapshot" in call_order, (
            "C5: _execute_refresh() must call _create_snapshot()."
        )
        assert call_order.index("_index_source") < call_order.index(
            "_create_snapshot"
        ), "C5: _index_source() must be called before _create_snapshot()."


# ---------------------------------------------------------------------------
# Tests: snapshot_manager integration (Story #1034 Commit 3)
# ---------------------------------------------------------------------------


class TestSnapshotManagerIntegration:
    """Verify _create_snapshot delegates to snapshot_manager when injected."""

    def test_create_snapshot_uses_snapshot_manager_when_injected(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        registry,
        source_repo,
    ):
        """
        Story #1034 C3: When snapshot_manager is injected, _create_snapshot must
        call snapshot_manager.create_snapshot(repo_name, source_path) instead of
        spawning subprocess cp --reflink=auto.
        """
        fake_versioned_path = str(
            golden_repos_dir / ".versioned" / "test-repo" / "v_9999999"
        )
        mock_snapshot_manager = MagicMock()
        mock_snapshot_manager.create_snapshot.return_value = fake_versioned_path

        # Create the versioned path so Step 5 validation passes
        Path(fake_versioned_path).mkdir(parents=True, exist_ok=True)
        (Path(fake_versioned_path) / ".code-indexer" / "index").mkdir(
            parents=True, exist_ok=True
        )

        sched = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=query_tracker,
            cleanup_manager=cleanup_manager,
            registry=registry,
            snapshot_manager=mock_snapshot_manager,
        )

        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "git@github.com:org/repo.git",
            str(source_repo),
        )

        cp_called = []

        def mock_run(cmd, **kwargs):
            if cmd[0] == "cp" and "--reflink=auto" in cmd:
                cp_called.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=mock_run):
            result = sched._create_snapshot(
                alias_name="test-repo-global", source_path=str(source_repo)
            )

        # snapshot_manager.create_snapshot must have been called
        mock_snapshot_manager.create_snapshot.assert_called_once_with(
            "test-repo", str(source_repo)
        )

        # cp --reflink=auto must NOT have been called directly
        assert cp_called == [], (
            f"Story #1034 C3: cp --reflink=auto must NOT be called when snapshot_manager "
            f"is injected. Got calls: {cp_called}"
        )

        assert result == fake_versioned_path, (
            f"_create_snapshot must return the path from snapshot_manager. "
            f"Expected {fake_versioned_path}, got {result}"
        )

    def test_create_snapshot_raises_wiring_bug_when_snapshot_manager_is_none(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        registry,
        source_repo,
    ):
        """
        Story #1034 C3: When snapshot_manager is None, _create_snapshot must raise
        RuntimeError with 'wiring bug in lifespan.py' (fail-loud per Codex B4).
        """
        sched = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=query_tracker,
            cleanup_manager=cleanup_manager,
            registry=registry,
            snapshot_manager=None,
        )

        with pytest.raises(RuntimeError, match="wiring bug in lifespan.py"):
            sched._create_snapshot(
                alias_name="test-repo-global", source_path=str(source_repo)
            )
