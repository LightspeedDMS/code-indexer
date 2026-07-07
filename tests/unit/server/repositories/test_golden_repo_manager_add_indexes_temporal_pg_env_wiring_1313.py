"""Bug #1313 round-4 (Codex Finding 1): a SECOND, previously-unwired
golden_repo_manager.py subprocess launch site.

Root cause: GoldenRepoManager.add_indexes_to_golden_repo() (reached via the
admin/MCP `add_golden_repo_index(index_type="temporal")` path) spawns a
CHILD `cidx index --index-commits` subprocess through its OWN local
`_run_with_popen_progress` closure -- a DIFFERENT helper than the
`_run_popen`/`_run_popen_with_telemetry` pair already wired in
`_execute_post_clone_workflow` (Bug #1313 round-3). That closure never
passed CIDX_TEMPORAL_PG_BOOTSTRAP_DIR, so this launch site silently used
SQLite even in cluster/postgres mode.

Mirrors test_golden_repo_manager_temporal_pg_env_wiring_1313.py's approach,
adapted to this method's background_worker + submit_job.side_effect pattern
(mirrors test_golden_repo_manager_scheduler_wiring.py's construction style).
"""

from __future__ import annotations

from unittest.mock import Mock, patch


from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.utils.config_manager import ServerConfig


def _make_manager(tmp_path, temporal_options=None):
    """Build a minimal GoldenRepoManager with a real repo_path on disk."""
    with patch.object(GoldenRepoManager, "__init__", lambda self, *a, **kw: None):
        manager = GoldenRepoManager.__new__(GoldenRepoManager)

    repo_path = tmp_path / "golden-repos" / "test-repo"
    (repo_path / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)

    manager.data_dir = str(tmp_path)
    manager.golden_repos_dir = str(tmp_path / "golden-repos")

    golden_repo = Mock()
    golden_repo.alias = "test-repo"
    golden_repo.clone_path = str(repo_path)
    golden_repo.temporal_options = temporal_options or {}
    golden_repo.enable_temporal = False

    manager.golden_repos = {"test-repo": golden_repo}
    manager.get_actual_repo_path = Mock(return_value=str(repo_path))
    manager._sqlite_backend = Mock()
    manager._sqlite_backend.update_enable_temporal = Mock(return_value=True)
    manager._global_repos_backend = Mock()

    captured_workers = []

    def capture_and_run(operation_type, func, submitter_username, **kwargs):
        captured_workers.append(func)
        return "job-add-indexes-test"

    manager.background_job_manager = Mock()
    manager.background_job_manager.submit_job.side_effect = capture_and_run
    manager._captured_workers = captured_workers
    manager._refresh_scheduler = None

    return manager, repo_path


def _run_captured_worker(manager) -> None:
    assert len(manager._captured_workers) == 1
    manager._captured_workers[0]()


def _mock_subprocess_run(command, **kwargs):
    return Mock(returncode=0, stdout="", stderr="")


class TestAddIndexesTemporalPopenGetsPostgresEnvInClusterMode:
    def test_temporal_command_receives_env_with_bootstrap_dir_var_in_postgres_mode(
        self, tmp_path
    ):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
        )

        manager, repo_path = _make_manager(tmp_path)
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
            patch(
                "code_indexer.server.repositories.golden_repo_manager.subprocess.run",
                side_effect=_mock_subprocess_run,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=_fake_run_with_popen_progress,
            ),
            patch(
                "code_indexer.server.services.config_service.get_config_service"
            ) as mock_get_cfg_svc,
        ):
            mock_get_cfg_svc.return_value.get_config.return_value = server_config

            manager.add_indexes_to_golden_repo(
                alias="test-repo", index_types=["temporal"]
            )
            _run_captured_worker(manager)

        by_phase = {c["phase_name"]: c["env"] for c in calls}
        assert "temporal" in by_phase, f"expected a temporal-phase call, got: {calls}"
        assert by_phase["temporal"] is not None
        assert by_phase["temporal"][TEMPORAL_PG_BOOTSTRAP_DIR_ENV] == "/opt/cidx-server"

    def test_semantic_command_always_receives_env_none_even_in_postgres_mode(
        self, tmp_path
    ):
        manager, repo_path = _make_manager(tmp_path)
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
            patch(
                "code_indexer.server.repositories.golden_repo_manager.subprocess.run",
                side_effect=_mock_subprocess_run,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=_fake_run_with_popen_progress,
            ),
            patch(
                "code_indexer.server.services.config_service.get_config_service"
            ) as mock_get_cfg_svc,
        ):
            mock_get_cfg_svc.return_value.get_config.return_value = server_config

            manager.add_indexes_to_golden_repo(
                alias="test-repo", index_types=["semantic"]
            )
            _run_captured_worker(manager)

        by_phase = {c["phase_name"]: c["env"] for c in calls}
        assert by_phase["semantic"] is None, (
            "the semantic Popen call must NEVER receive the PG bootstrap env "
            "-- only the temporal call is postgres-aware"
        )

    def test_temporal_command_receives_env_none_in_sqlite_mode(self, tmp_path):
        manager, repo_path = _make_manager(tmp_path)
        server_config = ServerConfig(
            server_dir="/opt/cidx-server", storage_mode="sqlite"
        )

        calls = []

        def _fake_run_with_popen_progress(*, command, phase_name, env=None, **kwargs):
            calls.append({"phase_name": phase_name, "env": env})
            return 100

        with (
            patch(
                "code_indexer.server.repositories.golden_repo_manager.subprocess.run",
                side_effect=_mock_subprocess_run,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=_fake_run_with_popen_progress,
            ),
            patch(
                "code_indexer.server.services.config_service.get_config_service"
            ) as mock_get_cfg_svc,
        ):
            mock_get_cfg_svc.return_value.get_config.return_value = server_config

            manager.add_indexes_to_golden_repo(
                alias="test-repo", index_types=["temporal"]
            )
            _run_captured_worker(manager)

        by_phase = {c["phase_name"]: c["env"] for c in calls}
        assert by_phase["temporal"] is None, (
            "sqlite/solo mode must be byte-unchanged: temporal Popen call "
            "must receive env=None"
        )
