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
def scheduler(golden_repos_dir, config_mgr, query_tracker, cleanup_manager, registry):
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        registry=registry,
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
# Tests: call order — index source FIRST, then CoW clone
# ---------------------------------------------------------------------------


class TestCallOrder:
    """Verify cidx index runs on source before CoW clone happens."""

    def test_cidx_index_fts_runs_before_cow_clone(
        self, scheduler, registry, source_repo
    ):
        """
        AC: cidx index --fts runs on golden repo source path BEFORE CoW clone.

        Story #229 requires indexing on the source so the snapshot inherits
        the index via CoW reflink — not the other way around.
        """
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "git@github.com:org/repo.git",
            str(source_repo),
        )

        call_sequence = []

        def mock_run(cmd, **kwargs):
            cwd = kwargs.get("cwd", "")
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""

            if cmd[0] == "cp" and "--reflink=auto" in cmd:
                call_sequence.append(("cow_clone", str(cwd)))
                shutil.copytree(cmd[-2], cmd[-1])

            elif cmd[:2] == ["cidx", "index"] and "--fts" in cmd:
                call_sequence.append(("cidx_index_fts", str(cwd)))
                (Path(cwd) / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)

            elif cmd[:2] == ["cidx", "fix-config"]:
                call_sequence.append(("fix_config", str(cwd)))

            return result

        with patch("subprocess.run", side_effect=mock_run):
            scheduler._index_source(alias_name="test-repo-global", source_path=str(source_repo))
            scheduler._create_snapshot(alias_name="test-repo-global", source_path=str(source_repo))

        index_positions = [
            i for i, (name, _) in enumerate(call_sequence) if name == "cidx_index_fts"
        ]
        cow_positions = [
            i for i, (name, _) in enumerate(call_sequence) if name == "cow_clone"
        ]

        assert index_positions, "cidx index --fts was never called"
        assert cow_positions, "CoW clone (cp --reflink=auto) was never called"

        first_index = min(index_positions)
        first_cow = min(cow_positions)
        assert first_index < first_cow, (
            f"cidx index --fts (pos={first_index}) must run BEFORE CoW clone (pos={first_cow}). "
            "Story #229 requires indexing source first."
        )

    def test_cidx_index_fts_cwd_is_source_path(self, scheduler, registry, source_repo):
        """
        AC: cidx index --fts must be invoked with cwd=source_path.
        """
        registry.register_global_repo(
            "cwd-test-repo",
            "cwd-test-repo-global",
            "git@github.com:org/repo.git",
            str(source_repo),
        )

        index_cwds = []

        def mock_run(cmd, **kwargs):
            cwd = kwargs.get("cwd", "")
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""

            if cmd[:2] == ["cidx", "index"] and "--fts" in cmd:
                index_cwds.append(str(cwd))
                (Path(cwd) / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)

            return result

        with patch("subprocess.run", side_effect=mock_run):
            scheduler._index_source(
                alias_name="cwd-test-repo-global", source_path=str(source_repo)
            )

        assert index_cwds, "cidx index --fts was not called by _index_source()"
        assert index_cwds[0] == str(source_repo), (
            f"cidx index --fts cwd must be source_path={source_repo}, got cwd={index_cwds[0]}."
        )


# ---------------------------------------------------------------------------
# Tests: fix-config — clone only, never source
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
            elif cmd[:2] == ["cidx", "index"] and "--fts" in cmd:
                (Path(cwd) / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)

            return result

        with patch("subprocess.run", side_effect=mock_run):
            scheduler._index_source(
                alias_name="fix-config-test-global", source_path=str(source_repo)
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
                # (_create_snapshot does NOT run cidx index — only _index_source does)
                (Path(dst) / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)
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

        def mock_index_source(alias_name, source_path):
            call_order.append("_index_source")

        def mock_create_snapshot(alias_name, source_path):
            call_order.append("_create_snapshot")
            Path(fake_snapshot_path).mkdir(parents=True, exist_ok=True)
            return fake_snapshot_path

        with patch.object(scheduler, "_index_source", side_effect=mock_index_source):
            with patch.object(scheduler, "_create_snapshot", side_effect=mock_create_snapshot):
                with patch.object(scheduler.alias_manager, "swap_alias"):
                    with patch.object(scheduler.cleanup_manager, "schedule_cleanup"):
                        with patch.object(scheduler.registry, "update_refresh_timestamp"):
                            with patch.object(scheduler, "_detect_existing_indexes", return_value={}):
                                with patch.object(scheduler, "_reconcile_registry_with_filesystem"):
                                    with patch(
                                        "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
                                    ) as mock_gpu:
                                        mock_updater = MagicMock()
                                        mock_updater.has_changes.return_value = True
                                        mock_updater.get_source_path.return_value = str(source_repo)
                                        mock_gpu.return_value = mock_updater

                                        scheduler._execute_refresh(alias_name)

        assert "_index_source" in call_order, "C5: _execute_refresh() must call _index_source()."
        assert "_create_snapshot" in call_order, "C5: _execute_refresh() must call _create_snapshot()."
        assert call_order.index("_index_source") < call_order.index("_create_snapshot"), (
            "C5: _index_source() must be called before _create_snapshot()."
        )
