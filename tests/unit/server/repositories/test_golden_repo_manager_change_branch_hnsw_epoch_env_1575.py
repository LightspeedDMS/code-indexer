"""Bug #1575 Part C remediation (independent re-review): the `change_branch`
`cidx index --fts` spawn (``GoldenRepoManager._cb_cidx_index``) never set
``CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV`` at all -- unlike
``_execute_post_clone_workflow``'s semantic/temporal Popen calls (already
fixed), this spawn site inherited the unsafe enabled-by-default fallback
even in postgres/cluster mode.
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


@pytest.fixture
def mock_repo_manager(tmp_path):
    return GoldenRepoManager(data_dir=str(tmp_path))


def _run_cb_cidx_index_and_capture_env(mock_repo_manager, base_clone_path, server_config):
    captured = {}

    def _fake_subprocess_run(command, **kwargs):
        captured["env"] = kwargs.get("env")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("subprocess.run", side_effect=_fake_subprocess_run),
        patch(
            "code_indexer.server.services.config_service.get_config_service"
        ) as mock_get_cfg_svc,
    ):
        mock_get_cfg_svc.return_value.get_config.return_value = server_config
        mock_repo_manager._cb_cidx_index(base_clone_path)

    return captured["env"]


class TestChangeBranchCidxIndexGetsHnswEpochPostgresEnv:
    def test_receives_postgres_mode_var_when_cluster(self, mock_repo_manager, tmp_path):
        server_config = ServerConfig(
            server_dir=_FAKE_SERVER_DIR,
            storage_mode="postgres",
            postgres_dsn=_FAKE_POSTGRES_DSN,
        )

        env = _run_cb_cidx_index_and_capture_env(
            mock_repo_manager, str(tmp_path), server_config
        )

        assert env is not None
        assert_env_value(
            env,
            CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
            "1",
            msg=(
                "change_branch's `cidx index --fts` child must be told it is "
                "running in postgres/cluster mode so it disables the "
                "hnsw_sync epoch mechanism"
            ),
        )

    def test_omits_postgres_mode_var_when_sqlite(self, mock_repo_manager, tmp_path):
        server_config = ServerConfig(server_dir=_FAKE_SERVER_DIR, storage_mode="sqlite")

        env = _run_cb_cidx_index_and_capture_env(
            mock_repo_manager, str(tmp_path), server_config
        )

        assert env is not None
        assert_env_absent(
            env,
            CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
            msg="sqlite/solo mode must never set this var -- child defaults enabled",
        )
