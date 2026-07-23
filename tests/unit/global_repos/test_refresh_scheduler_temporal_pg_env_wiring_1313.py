"""Bug #1313 round-3: refresh_scheduler.py must thread the PG bootstrap env
var through the temporal `--index-commits` Popen call ONLY, in postgres mode.

Mirrors test_golden_repo_manager_temporal_pg_env_wiring_1313.py exactly, but
for the scheduled-refresh code path (RefreshScheduler._index_source), the
OTHER Popen call site that spawns a child `cidx index --index-commits`
subprocess. Root cause and fix are identical: the child never installed the
PostgreSQL temporal-metadata backend, so it silently used SQLite even in
cluster/postgres mode.

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

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.config import ConfigManager
from code_indexer.server.utils.config_manager import ServerConfig
from tests.utils.env_assertions import assert_env_absent


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
def mock_registry():
    registry = MagicMock()
    registry.get_global_repo.return_value = {
        "alias": "test-repo-global",
        "repo_url": "git@github.com:org/repo.git",
        "enable_temporal": True,
        "temporal_options": None,
        "enable_scip": False,
    }
    return registry


@pytest.fixture
def scheduler(
    golden_repos_dir, config_mgr, query_tracker, cleanup_manager, mock_registry
):
    sched = RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        registry=mock_registry,
    )
    return sched


@pytest.fixture
def source_repo(tmp_path):
    src = tmp_path / "source_repo"
    src.mkdir()
    (src / "README.md").write_text("# Test Repo")
    (src / ".git").mkdir()
    return src


def _make_fake_run_with_popen_progress(calls):
    def _fake(*, command, phase_name, env=None, **kwargs):
        calls.append({"phase_name": phase_name, "env": env})
        return 100

    return _fake


class TestTemporalPopenCallGetsPostgresEnvInClusterMode:
    def test_temporal_command_receives_env_with_bootstrap_dir_var_in_postgres_mode(
        self, scheduler, source_repo
    ):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
        )

        server_config = ServerConfig(
            server_dir="/opt/cidx-server",
            storage_mode="postgres",
            postgres_dsn="postgresql://user:pass@host/db",
        )

        calls: list = []
        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=_make_fake_run_with_popen_progress(calls),
            ),
            patch(
                "code_indexer.global_repos.refresh_scheduler.get_config_service"
            ) as mock_get_cfg_svc,
        ):
            mock_get_cfg_svc.return_value.get_config.return_value = server_config

            scheduler._index_source(
                alias_name="test-repo-global",
                source_path=str(source_repo),
            )

        by_phase = {c["phase_name"]: c["env"] for c in calls}
        assert "temporal" in by_phase, f"expected a temporal-phase call, got: {calls}"
        assert by_phase["temporal"] is not None
        assert by_phase["temporal"][TEMPORAL_PG_BOOTSTRAP_DIR_ENV] == "/opt/cidx-server"

    def test_semantic_command_receives_sanitized_non_none_env_in_postgres_mode(
        self, scheduler, source_repo
    ):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
        )

        server_config = ServerConfig(
            server_dir="/opt/cidx-server",
            storage_mode="postgres",
            postgres_dsn="postgresql://user:pass@host/db",
        )

        calls: list = []
        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=_make_fake_run_with_popen_progress(calls),
            ),
            patch(
                "code_indexer.global_repos.refresh_scheduler.get_config_service"
            ) as mock_get_cfg_svc,
        ):
            mock_get_cfg_svc.return_value.get_config.return_value = server_config

            scheduler._index_source(
                alias_name="test-repo-global",
                source_path=str(source_repo),
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
        self, scheduler, source_repo
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

        calls: list = []
        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=_make_fake_run_with_popen_progress(calls),
            ),
            patch(
                "code_indexer.global_repos.refresh_scheduler.get_config_service"
            ) as mock_get_cfg_svc,
        ):
            mock_get_cfg_svc.return_value.get_config.return_value = server_config

            scheduler._index_source(
                alias_name="test-repo-global",
                source_path=str(source_repo),
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
