"""Bug #1313 round-4 Finding 3 (discovered via exhaustive sweep, NOT in
Codex's original two-finding list): a FIFTH server-side subprocess launch
site of `cidx index --index-commits`.

Root cause: ActivatedRepoIndexManager._execute_temporal_indexing (manual
reindex of an activated repo, reached via trigger_reindex/_execute_single_index_type)
spawns a CHILD `cidx index --index-commits` subprocess through
_run_subprocess_with_telemetry -> subprocess.run(args, cwd=repo_path,
capture_output=True, text=True) -- with NO env= kwarg at all, so the child
inherits this SERVER PROCESS's own environment, which never carries
CIDX_TEMPORAL_PG_BOOTSTRAP_DIR. This silently used SQLite even in
cluster/postgres mode -- the same root cause as the four other sites, a
fifth entry point.

Fix: compute build_temporal_child_env(get_config_service().get_config())
and pass it as env= ONLY for the temporal subprocess.run call; the sibling
FTS call (_execute_fts_indexing) stays untouched (env=None / inherited).

Bug #1325 update: _run_subprocess_with_telemetry now ALWAYS sanitizes the
resolved env via build_cidx_subprocess_env (absolutizing any relative
PYTHONPATH entry so the child does not shadow installed dependencies with
repo-local packages) -- so the FTS call and the sqlite-mode temporal call
no longer receive raw env=None; they receive a sanitized, non-None env that
still never carries the temporal-only PG bootstrap var.
"""

from __future__ import annotations

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
from tests.utils.env_assertions import assert_env_absent


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


class TestExecuteTemporalIndexingGetsPostgresEnvInClusterMode:
    def test_temporal_command_receives_env_with_bootstrap_dir_var_in_postgres_mode(
        self, index_manager, tmp_path, capturing_subprocess_run
    ):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
        )

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
        env = captured_calls[0]["env"]
        assert env is not None
        assert env.get(TEMPORAL_PG_BOOTSTRAP_DIR_ENV) == "/opt/cidx-server"

    def test_fts_command_receives_sanitized_env_without_pg_bootstrap_var(
        self, index_manager, tmp_path, capturing_subprocess_run
    ):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
        )

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

            index_manager._execute_fts_indexing(str(tmp_path), clear=False)

        assert len(captured_calls) == 1
        fts_env = captured_calls[0]["env"]
        assert fts_env is not None, (
            "Bug #1325: the FTS subprocess.run call must receive a sanitized "
            "env (build_cidx_subprocess_env), never raw None"
        )
        assert_env_absent(
            fts_env,
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
            msg=(
                "the FTS subprocess.run call must NEVER receive the temporal-only "
                "PG bootstrap var -- only the temporal call is postgres-aware for "
                "that var"
            ),
        )

    def test_temporal_command_receives_sanitized_env_without_pg_bootstrap_var_in_sqlite_mode(
        self, index_manager, tmp_path, capturing_subprocess_run
    ):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
        )

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
        temporal_env = captured_calls[0]["env"]
        assert temporal_env is not None, (
            "Bug #1325: sqlite/solo mode temporal subprocess.run call must "
            "receive a sanitized env (build_cidx_subprocess_env), never raw "
            "None"
        )
        assert_env_absent(
            temporal_env,
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
            msg="sqlite/solo mode must never carry the PG bootstrap var",
        )
