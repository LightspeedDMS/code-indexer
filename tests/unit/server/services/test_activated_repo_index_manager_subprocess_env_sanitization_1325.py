"""Bug #1325: cidx subprocesses spawned by ActivatedRepoIndexManager with
cwd=<repo_path> must never inherit a RELATIVE PYTHONPATH unchanged from the
server process.

Root cause: when the server is launched via the documented dev command
(`PYTHONPATH=./src python3 -m uvicorn code_indexer.server.app:app`), that
relative PYTHONPATH entry is inherited unchanged by `cidx index`/`cidx scip
generate` child subprocesses. Because PYTHONPATH resolution is relative to
the CURRENT process's cwd, and these children run with cwd=<repo_path>, the
relative entry re-anchors into the repo directory. If the repo has its own
`src/`-layout package colliding with a real cidx dependency (e.g. `click`),
the repo's package shadows the installed dependency and cidx's own imports
break.

Fix: _run_subprocess_with_telemetry now ALWAYS sanitizes env via
build_cidx_subprocess_env (even when the caller passes env=None, the
semantic/FTS default case; when the caller passes a temporal env dict, its
PYTHONPATH is also absolutized). _execute_scip_indexing (which bypasses
_run_subprocess_with_telemetry) gets the same treatment directly.

These tests prove ALL FOUR subprocess.run call sites in this manager
receive an absolutized PYTHONPATH.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)
from code_indexer.server.utils.config_manager import ServerConfig

_RELATIVE_PYTHONPATH = "./src"


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def mock_background_job_manager():
    manager = Mock(spec=BackgroundJobManager)
    manager.submit_job = Mock(return_value=str(uuid.uuid4()))
    manager.list_jobs = Mock(return_value={"jobs": [], "total": 0})
    return manager


@pytest.fixture
def mock_activated_repo_manager(temp_data_dir):
    manager = Mock()
    repo_path = str(Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo")
    manager.get_activated_repo_path = Mock(return_value=repo_path)
    return manager


@pytest.fixture
def index_manager(
    temp_data_dir, mock_background_job_manager, mock_activated_repo_manager
):
    return ActivatedRepoIndexManager(
        data_dir=temp_data_dir,
        background_job_manager=mock_background_job_manager,
        activated_repo_manager=mock_activated_repo_manager,
    )


@pytest.fixture
def capturing_subprocess_run():
    """Shared subprocess.run capture helper: records (args, env) per call."""
    captured_calls: list = []

    def _run(args, env=None, **kwargs):
        captured_calls.append({"args": args, "env": env})
        return Mock(returncode=0, stdout="", stderr="")

    return _run, captured_calls


def _assert_absolutized(env, expected_abs):
    assert env is not None, "subprocess.run call must receive a sanitized env"
    assert env.get("PYTHONPATH") == expected_abs


class TestActivatedRepoIndexManagerSanitizesPythonPath:
    def test_semantic_indexing_receives_absolutized_pythonpath(
        self, monkeypatch, index_manager, tmp_path, capturing_subprocess_run
    ):
        monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
        expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)
        run_fn, captured_calls = capturing_subprocess_run

        with patch(
            "code_indexer.server.services.activated_repo_index_manager"
            ".run_cancellable_subprocess",
            side_effect=run_fn,
        ):
            index_manager._execute_semantic_indexing(str(tmp_path), clear=False)

        assert len(captured_calls) == 1
        _assert_absolutized(captured_calls[0]["env"], expected_abs)

    def test_fts_indexing_receives_absolutized_pythonpath(
        self, monkeypatch, index_manager, tmp_path, capturing_subprocess_run
    ):
        monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
        expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)
        run_fn, captured_calls = capturing_subprocess_run

        with patch(
            "code_indexer.server.services.activated_repo_index_manager"
            ".run_cancellable_subprocess",
            side_effect=run_fn,
        ):
            index_manager._execute_fts_indexing(str(tmp_path), clear=False)

        assert len(captured_calls) == 1
        _assert_absolutized(captured_calls[0]["env"], expected_abs)

    def test_temporal_indexing_receives_absolutized_pythonpath_in_sqlite_mode(
        self, monkeypatch, index_manager, tmp_path, capturing_subprocess_run
    ):
        monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
        expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)
        server_config = ServerConfig(
            server_dir="/opt/cidx-server", storage_mode="sqlite"
        )
        run_fn, captured_calls = capturing_subprocess_run

        with (
            patch(
                "code_indexer.server.services.activated_repo_index_manager"
                ".run_cancellable_subprocess",
                side_effect=run_fn,
            ),
            patch(
                "code_indexer.server.services.activated_repo_index_manager.get_config_service"
            ) as mock_get_cfg_svc,
        ):
            mock_get_cfg_svc.return_value.get_config.return_value = server_config
            index_manager._execute_temporal_indexing(str(tmp_path), clear=False)

        assert len(captured_calls) == 1
        _assert_absolutized(captured_calls[0]["env"], expected_abs)

    def test_temporal_indexing_receives_absolutized_pythonpath_in_postgres_mode(
        self, monkeypatch, index_manager, tmp_path, capturing_subprocess_run
    ):
        monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
        expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)
        server_config = ServerConfig(
            server_dir="/opt/cidx-server",
            storage_mode="postgres",
            postgres_dsn="postgresql://user:pass@host/db",
        )
        run_fn, captured_calls = capturing_subprocess_run

        with (
            patch(
                "code_indexer.server.services.activated_repo_index_manager"
                ".run_cancellable_subprocess",
                side_effect=run_fn,
            ),
            patch(
                "code_indexer.server.services.activated_repo_index_manager.get_config_service"
            ) as mock_get_cfg_svc,
        ):
            mock_get_cfg_svc.return_value.get_config.return_value = server_config
            index_manager._execute_temporal_indexing(str(tmp_path), clear=False)

        assert len(captured_calls) == 1
        _assert_absolutized(captured_calls[0]["env"], expected_abs)

    def test_scip_indexing_receives_absolutized_pythonpath(
        self, monkeypatch, index_manager, tmp_path, capturing_subprocess_run
    ):
        monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
        expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)
        run_fn, captured_calls = capturing_subprocess_run

        with patch(
            "code_indexer.server.services.activated_repo_index_manager.subprocess.run",
            side_effect=run_fn,
        ):
            index_manager._execute_scip_indexing(str(tmp_path), clear=False)

        assert len(captured_calls) == 1
        _assert_absolutized(captured_calls[0]["env"], expected_abs)
