"""Bug #1575 Part C review Defect 3a bypass 3: the semantic `cidx index
--fts` Popen call in ``_execute_post_clone_workflow`` must thread a NEW
env var (``CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE``) so the spawned CLI child
can resolve ``hnsw_sync_epoch_enabled=False`` in postgres/cluster mode --
it has no ``app.state`` to inspect via ``is_postgres_storage_mode()``
itself.

Mirrors test_golden_repo_manager_temporal_pg_env_wiring_1313.py's approach
exactly (same mocking style: real ``_execute_post_clone_workflow`` call,
``subprocess.run`` and ``run_with_popen_progress`` patched, real
``get_config_service()`` mocked to return a real ``ServerConfig``).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.utils.config_manager import ServerConfig
from code_indexer.storage.shared.hnsw_sync_state import (
    CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
)
from tests.utils.env_assertions import assert_env_absent, assert_env_value

_FAKE_SERVER_DIR = "/opt/cidx-server"
_FAKE_POSTGRES_DSN = "postgresql://user:pass@host/db"
_FAKE_PROGRESS_PERCENT_DONE = 100


@pytest.fixture
def mock_repo_manager(tmp_path):
    return GoldenRepoManager(data_dir=str(tmp_path))


@pytest.fixture
def mock_clone_path(tmp_path):
    clone_path = tmp_path / "test-repo"
    clone_path.mkdir()
    return clone_path


def _mock_subprocess_run(command, **kwargs):
    return MagicMock(returncode=0, stdout="", stderr="")


def _run_workflow_and_capture_semantic_env(
    mock_repo_manager, mock_clone_path, server_config
):
    calls = []

    def _fake_run_with_popen_progress(*, command, phase_name, env=None, **kwargs):
        calls.append({"phase_name": phase_name, "env": env})
        return _FAKE_PROGRESS_PERCENT_DONE

    with (
        patch("subprocess.run", side_effect=_mock_subprocess_run),
        patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=_fake_run_with_popen_progress,
        ),
        patch(
            "code_indexer.server.services.config_service.get_config_service"
        ) as mock_get_cfg_svc,
    ):
        mock_get_cfg_svc.return_value.get_config.return_value = server_config

        mock_repo_manager._execute_post_clone_workflow(
            clone_path=str(mock_clone_path),
            force_init=False,
            enable_temporal=False,
            temporal_options=None,
        )

    by_phase = {c["phase_name"]: c["env"] for c in calls}
    return by_phase["semantic"]


def _run_workflow_and_capture_temporal_env(
    mock_repo_manager, mock_clone_path, server_config
):
    calls = []

    def _fake_run_with_popen_progress(*, command, phase_name, env=None, **kwargs):
        calls.append({"phase_name": phase_name, "env": env})
        return _FAKE_PROGRESS_PERCENT_DONE

    with (
        patch("subprocess.run", side_effect=_mock_subprocess_run),
        patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=_fake_run_with_popen_progress,
        ),
        patch(
            "code_indexer.server.services.config_service.get_config_service"
        ) as mock_get_cfg_svc,
    ):
        mock_get_cfg_svc.return_value.get_config.return_value = server_config

        mock_repo_manager._execute_post_clone_workflow(
            clone_path=str(mock_clone_path),
            force_init=False,
            enable_temporal=True,
            temporal_options=None,
        )

    by_phase = {c["phase_name"]: c["env"] for c in calls}
    return by_phase["temporal"]


class TestTemporalPopenGetsHnswEpochPostgresEnvInClusterMode:
    """Bug #1575 Part C review fix (Defect 3a, temporal-path bypass):
    temporal collections use the SAME FilesystemVectorStore/HNSWIndexManager
    machinery as semantic ones (per this project's own Bug #1529 notes), so
    the `cidx index --index-commits` temporal Popen child needs the SAME
    postgres-mode signal the semantic `--fts` child already receives.
    """

    def test_temporal_command_receives_postgres_mode_var_when_cluster(
        self, mock_repo_manager, mock_clone_path
    ):
        server_config = ServerConfig(
            server_dir=_FAKE_SERVER_DIR,
            storage_mode="postgres",
            postgres_dsn=_FAKE_POSTGRES_DSN,
        )

        temporal_env = _run_workflow_and_capture_temporal_env(
            mock_repo_manager, mock_clone_path, server_config
        )

        assert temporal_env is not None
        assert_env_value(
            temporal_env,
            CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
            "1",
            msg=(
                "the temporal Popen call must ALSO signal postgres mode to "
                "the child -- temporal collections build/rebuild an HNSW "
                "graph via the same mechanism as semantic collections, so "
                "omitting this var here leaves the AC46 cluster gate open "
                "for temporal specifically"
            ),
        )

    def test_temporal_command_omits_postgres_mode_var_when_sqlite(
        self, mock_repo_manager, mock_clone_path
    ):
        server_config = ServerConfig(server_dir=_FAKE_SERVER_DIR, storage_mode="sqlite")

        temporal_env = _run_workflow_and_capture_temporal_env(
            mock_repo_manager, mock_clone_path, server_config
        )

        assert temporal_env is not None
        assert_env_absent(
            temporal_env,
            CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
            msg="sqlite/solo mode must never set this var -- child defaults enabled",
        )


class TestSemanticPopenGetsHnswEpochPostgresEnvInClusterMode:
    def test_semantic_command_receives_postgres_mode_var_when_cluster(
        self, mock_repo_manager, mock_clone_path
    ):
        server_config = ServerConfig(
            server_dir=_FAKE_SERVER_DIR,
            storage_mode="postgres",
            postgres_dsn=_FAKE_POSTGRES_DSN,
        )

        semantic_env = _run_workflow_and_capture_semantic_env(
            mock_repo_manager, mock_clone_path, server_config
        )

        assert semantic_env is not None
        assert_env_value(
            semantic_env,
            CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
            "1",
            msg=(
                "the semantic Popen call must signal postgres mode to the "
                "child via this env var -- otherwise the spawned `cidx "
                "index --fts` child cannot know it must disable the "
                "hnsw_sync epoch mechanism (no app.state to inspect there)"
            ),
        )

    def test_semantic_command_omits_postgres_mode_var_when_sqlite(
        self, mock_repo_manager, mock_clone_path
    ):
        server_config = ServerConfig(server_dir=_FAKE_SERVER_DIR, storage_mode="sqlite")

        semantic_env = _run_workflow_and_capture_semantic_env(
            mock_repo_manager, mock_clone_path, server_config
        )

        assert semantic_env is not None
        assert_env_absent(
            semantic_env,
            CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
            msg="sqlite/solo mode must never set this var -- child defaults enabled",
        )
