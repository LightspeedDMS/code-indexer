"""Bug #1313 round-3: golden_repo_manager.py must thread the PG bootstrap env
var through the temporal `--index-commits` Popen call ONLY, in postgres mode.

Root cause: cluster temporal indexing runs in a CHILD `cidx index
--index-commits` subprocess spawned via Popen by
GoldenRepoManager._execute_post_clone_workflow. That subprocess never
inherited a signal telling it to install the PostgreSQL temporal-metadata
backend, so it silently fell back to SQLite even in cluster/postgres mode
(the exact NFS-backed bottleneck Bug #1313 exists to fix). The fix: the
parent (this manager) computes build_temporal_child_env(server_config) and
passes it as `env=` to run_with_popen_progress ONLY for the temporal Popen
call -- the semantic/FTS Popen call always stays env=None (untouched).

These are call-site wiring tests: run_with_popen_progress itself is mocked
(it already has its own dedicated tests in
test_progress_subprocess_runner.py); this file proves
_execute_post_clone_workflow computes and forwards the right env dict at the
right call site.

Bug #1325 update: the semantic/FTS Popen call no longer stays env=None --
every cidx subprocess call site now receives a sanitized env via
build_cidx_subprocess_env() (which absolutizes any relative PYTHONPATH entry
so the child does not shadow installed dependencies with clone-local
packages). The semantic call must still NEVER receive the temporal-only PG
bootstrap var; only the temporal call is postgres-aware for that var.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.utils.config_manager import ServerConfig
from tests.utils.env_assertions import assert_env_absent


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


class TestTemporalPopenCallGetsPostgresEnvInClusterMode:
    def test_temporal_command_receives_env_with_bootstrap_dir_var_in_postgres_mode(
        self, mock_repo_manager, mock_clone_path
    ):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
        )

        server_config = ServerConfig(
            server_dir="/opt/cidx-server",
            storage_mode="postgres",
            postgres_dsn="postgresql://user:pass@host/db",
        )

        calls = []

        def _fake_run_with_popen_progress(*, command, phase_name, env=None, **kwargs):
            calls.append({"phase_name": phase_name, "env": env})
            return 100

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
        assert "temporal" in by_phase, f"expected a temporal-phase call, got: {calls}"
        assert by_phase["temporal"] is not None
        assert by_phase["temporal"][TEMPORAL_PG_BOOTSTRAP_DIR_ENV] == "/opt/cidx-server"

    def test_semantic_command_receives_sanitized_non_none_env_in_postgres_mode(
        self, mock_repo_manager, mock_clone_path
    ):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
        )

        server_config = ServerConfig(
            server_dir="/opt/cidx-server",
            storage_mode="postgres",
            postgres_dsn="postgresql://user:pass@host/db",
        )

        calls = []

        def _fake_run_with_popen_progress(*, command, phase_name, env=None, **kwargs):
            calls.append({"phase_name": phase_name, "env": env})
            return 100

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
        semantic_env = by_phase["semantic"]
        assert semantic_env is not None, (
            "Bug #1325: the semantic/FTS Popen call must receive a sanitized "
            "env (build_cidx_subprocess_env), never raw None"
        )
        assert_env_absent(
            semantic_env,
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
            msg=(
                "the semantic/FTS Popen call must NEVER receive the temporal-only "
                "PG bootstrap var -- only the temporal call is postgres-aware "
                "for that var"
            ),
        )

    def test_temporal_command_receives_non_none_env_with_embedding_stats_var_in_sqlite_mode(
        self, mock_repo_manager, mock_clone_path
    ):
        """Story #1418: unlike the temporal-only PG bootstrap var (which
        stays gated on postgres mode), CIDX_EMBEDDING_STATS_BOOTSTRAP_DIR
        fires UNCONDITIONALLY -- so the temporal Popen call's env is no
        longer raw None in sqlite mode, even though the temporal-specific
        var is still correctly absent."""
        from code_indexer.storage.temporal_metadata_backend_registry import (
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
        )
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            EMBEDDING_STATS_BOOTSTRAP_DIR_ENV,
        )

        server_config = ServerConfig(
            server_dir="/opt/cidx-server", storage_mode="sqlite"
        )

        calls = []

        def _fake_run_with_popen_progress(*, command, phase_name, env=None, **kwargs):
            calls.append({"phase_name": phase_name, "env": env})
            return 100

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
        temporal_env = by_phase["temporal"]
        assert temporal_env is not None, (
            "Story #1418: the embedding-stats bootstrap var fires "
            "unconditionally, so env is no longer raw None even in sqlite "
            "mode"
        )
        assert temporal_env[EMBEDDING_STATS_BOOTSTRAP_DIR_ENV] == "/opt/cidx-server"
        assert_env_absent(
            temporal_env,
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
            msg="sqlite mode must NOT receive the temporal-only PG bootstrap var",
        )
