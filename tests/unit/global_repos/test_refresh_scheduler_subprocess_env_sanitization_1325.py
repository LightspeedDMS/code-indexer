"""Bug #1325 (code-review follow-up): every `cidx` subprocess spawned by
RefreshScheduler with cwd=<source_or_versioned_or_master_path> must never
inherit a RELATIVE PYTHONPATH unchanged from the server process.

Root cause and fix are identical to the golden-repo registration path (see
test_golden_repo_manager_subprocess_env_sanitization_1325.py): when the
server is launched via the documented dev command
(`PYTHONPATH=./src python3 -m uvicorn code_indexer.server.app:app`), that
relative PYTHONPATH entry is inherited unchanged by `cidx` child
subprocesses. Because PYTHONPATH resolution is relative to the CURRENT
process's cwd, and these children run with cwd=<clone/repo path>, the
relative entry re-anchors into the child's cwd. If that directory has its
own `src/`-layout package colliding with a real cidx dependency (e.g.
`click`), the clone's package shadows the installed dependency.

The code-review finding for Bug #1325 was that this fix was applied ONLY at
the golden-repo registration/add-index call sites, not at the golden-repo
REFRESH flow's own spawn sites (this module). This test file proves the fix
now covers ALL FIVE cidx subprocess call sites in refresh_scheduler.py:

- _index_source(): semantic/FTS Popen call (`cidx index --fts ...`)
- _index_source(): SCIP subprocess.run call (`cidx scip generate`)
- _create_snapshot(): fix-config subprocess.run call on the versioned clone
- _repair_uninitialized_local_repo(): cidx init subprocess.run call
- _restore_master_from_versioned(): fix-config subprocess.run call on the
  restored master
"""

from __future__ import annotations

import os
import time
import shutil
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.config import ConfigManager

_RELATIVE_PYTHONPATH = "./src"


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
def source_repo(tmp_path):
    src = tmp_path / "source_repo"
    src.mkdir()
    (src / "README.md").write_text("# Test Repo")
    (src / ".git").mkdir()
    return src


def _capture_run(calls):
    def _run(cmd, **kwargs):
        calls.append({"cmd": list(cmd), "kwargs": kwargs})
        result = Mock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    return _run


def _capture_popen(calls):
    def _fake(*, command, phase_name, env=None, **kwargs):
        calls.append({"phase_name": phase_name, "env": env})
        return 100

    return _fake


class TestIndexSourceSemanticPopenSanitizesPythonPath:
    """_index_source(): the semantic/FTS Popen call must receive an
    absolutized PYTHONPATH instead of raw env=None."""

    @pytest.fixture
    def mock_registry(self):
        registry = MagicMock()
        registry.get_global_repo.return_value = {
            "alias": "test-repo-global",
            "repo_url": "git@github.com:org/repo.git",
            "enable_temporal": False,
            "temporal_options": None,
            "enable_scip": False,
        }
        return registry

    @pytest.fixture
    def scheduler(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        mock_registry,
    ):
        return RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=query_tracker,
            cleanup_manager=cleanup_manager,
            registry=mock_registry,
        )

    def test_semantic_popen_call_receives_absolutized_pythonpath(
        self, monkeypatch, scheduler, source_repo
    ):
        monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
        expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)

        popen_calls: list = []
        with patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=_capture_popen(popen_calls),
        ):
            scheduler._index_source(
                alias_name="test-repo-global", source_path=str(source_repo)
            )

        by_phase = {c["phase_name"]: c["env"] for c in popen_calls}
        assert "semantic" in by_phase, (
            f"expected a semantic-phase call, got: {popen_calls}"
        )
        semantic_env = by_phase["semantic"]
        assert semantic_env is not None, (
            "Bug #1325: the semantic/FTS Popen call must receive a "
            "sanitized env, never raw None"
        )
        assert semantic_env["PYTHONPATH"] == expected_abs


class TestIndexSourceScipGenerateSanitizesPythonPath:
    """_index_source(): the SCIP subprocess.run call must receive an
    absolutized PYTHONPATH."""

    @pytest.fixture
    def mock_registry(self):
        registry = MagicMock()
        registry.get_global_repo.return_value = {
            "alias": "test-repo-global",
            "repo_url": "git@github.com:org/repo.git",
            "enable_temporal": False,
            "temporal_options": None,
            "enable_scip": True,
        }
        return registry

    @pytest.fixture
    def scheduler(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        mock_registry,
    ):
        return RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=query_tracker,
            cleanup_manager=cleanup_manager,
            registry=mock_registry,
        )

    def test_scip_generate_receives_absolutized_pythonpath(
        self, monkeypatch, scheduler, source_repo
    ):
        monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
        expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)

        run_calls: list = []
        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=_capture_popen([]),
            ),
            patch(
                "code_indexer.global_repos.refresh_scheduler.subprocess.run",
                side_effect=_capture_run(run_calls),
            ),
        ):
            scheduler._index_source(
                alias_name="test-repo-global", source_path=str(source_repo)
            )

        scip_calls = [c for c in run_calls if c["cmd"][:2] == ["cidx", "scip"]]
        assert scip_calls, f"expected a 'cidx scip generate' call, got: {run_calls}"
        scip_env = scip_calls[0]["kwargs"].get("env")
        assert scip_env is not None, "cidx scip generate must receive a sanitized env"
        assert scip_env["PYTHONPATH"] == expected_abs


class TestCreateSnapshotFixConfigSanitizesPythonPath:
    """_create_snapshot(): the `cidx fix-config --force` call on the CoW
    clone must receive an absolutized PYTHONPATH."""

    @pytest.fixture
    def mock_snapshot_manager(self, golden_repos_dir):
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
            (versioned_path / ".code-indexer" / "index").mkdir(
                parents=True, exist_ok=True
            )
            return str(versioned_path)

        mgr.create_snapshot.side_effect = _create_snapshot
        return mgr

    @pytest.fixture
    def scheduler(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        mock_snapshot_manager,
    ):
        return RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=query_tracker,
            cleanup_manager=cleanup_manager,
            snapshot_manager=mock_snapshot_manager,
        )

    def test_fix_config_receives_absolutized_pythonpath(
        self, monkeypatch, scheduler, source_repo
    ):
        monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
        expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)

        run_calls: list = []
        with (
            patch(
                "code_indexer.global_repos.refresh_scheduler.wait_for_nfs_visibility",
            ),
            patch(
                "code_indexer.global_repos.refresh_scheduler.subprocess.run",
                side_effect=_capture_run(run_calls),
            ),
        ):
            scheduler._create_snapshot("myrepo-global", str(source_repo))

        fix_config_calls = [
            c for c in run_calls if c["cmd"][:2] == ["cidx", "fix-config"]
        ]
        assert fix_config_calls, f"expected a 'cidx fix-config' call, got: {run_calls}"
        fix_env = fix_config_calls[0]["kwargs"].get("env")
        assert fix_env is not None, "cidx fix-config must receive a sanitized env"
        assert fix_env["PYTHONPATH"] == expected_abs


class TestRepairUninitializedLocalRepoSanitizesPythonPath:
    """_repair_uninitialized_local_repo(): the `cidx init` repair call must
    receive an absolutized PYTHONPATH."""

    @pytest.fixture
    def scheduler(self, golden_repos_dir, config_mgr, query_tracker, cleanup_manager):
        return RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=query_tracker,
            cleanup_manager=cleanup_manager,
        )

    def test_cidx_init_receives_absolutized_pythonpath(
        self, monkeypatch, scheduler, source_repo
    ):
        monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
        expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)

        run_calls: list = []
        with patch(
            "code_indexer.global_repos.refresh_scheduler.subprocess.run",
            side_effect=_capture_run(run_calls),
        ):
            result = scheduler._repair_uninitialized_local_repo(
                source_path=str(source_repo), alias_name="test-repo-global"
            )

        assert result is True
        assert len(run_calls) == 1
        init_env = run_calls[0]["kwargs"].get("env")
        assert init_env is not None, "cidx init repair must receive a sanitized env"
        assert init_env["PYTHONPATH"] == expected_abs


class TestRestoreMasterFromVersionedSanitizesPythonPath:
    """_restore_master_from_versioned(): the `cidx fix-config --force` call
    on the restored master must receive an absolutized PYTHONPATH."""

    @pytest.fixture
    def mock_clone_backend(self):
        backend = Mock()
        backend.create_clone_at_path.return_value = "/restored/path"
        return backend

    @pytest.fixture
    def mock_snapshot_manager(self, mock_clone_backend, golden_repos_dir):
        sm = Mock()
        sm._clone_backend = mock_clone_backend

        def _latest_snapshot(alias):
            repo_name = alias.removesuffix("-global")
            ns_dir = Path(golden_repos_dir) / ".versioned" / repo_name
            if not ns_dir.exists():
                return None
            for d in ns_dir.iterdir():
                if d.is_dir() and d.name.startswith("v_"):
                    return str(d)
            return None

        sm.latest_snapshot.side_effect = _latest_snapshot
        return sm

    @pytest.fixture
    def scheduler(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        mock_snapshot_manager,
    ):
        return RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=query_tracker,
            cleanup_manager=cleanup_manager,
            snapshot_manager=mock_snapshot_manager,
        )

    def test_fix_config_receives_absolutized_pythonpath(
        self, monkeypatch, scheduler, golden_repos_dir, tmp_path
    ):
        monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
        expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)

        versioned_dir = golden_repos_dir / ".versioned" / "myrepo" / "v_123"
        versioned_dir.mkdir(parents=True)

        master_path = tmp_path / "restored-master"

        run_calls: list = []
        with patch(
            "code_indexer.global_repos.refresh_scheduler.subprocess.run",
            side_effect=_capture_run(run_calls),
        ):
            result = scheduler._restore_master_from_versioned(
                alias_name="myrepo-global", master_path=master_path
            )

        assert result is True
        fix_config_calls = [
            c for c in run_calls if c["cmd"][:2] == ["cidx", "fix-config"]
        ]
        assert fix_config_calls, f"expected a 'cidx fix-config' call, got: {run_calls}"
        fix_env = fix_config_calls[0]["kwargs"].get("env")
        assert fix_env is not None, "cidx fix-config must receive a sanitized env"
        assert fix_env["PYTHONPATH"] == expected_abs
