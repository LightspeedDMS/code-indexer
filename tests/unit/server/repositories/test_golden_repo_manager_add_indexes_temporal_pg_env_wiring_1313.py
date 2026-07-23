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

Bug #1325 update: the semantic Popen call no longer stays env=None -- every
cidx subprocess call site now receives a sanitized env via
build_cidx_subprocess_env() (absolutizes any relative PYTHONPATH entry so
the child does not shadow installed dependencies with clone-local
packages). The semantic call must still NEVER receive the temporal-only PG
bootstrap var; only the temporal call is postgres-aware for that var.
"""

from __future__ import annotations

from unittest.mock import Mock, patch


from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.utils.config_manager import ServerConfig
from tests.utils.env_assertions import assert_env_absent


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
    # Bug #1316: add_indexes_to_golden_repo's background_worker now resolves
    # `repo` via _resolve_golden_repo_authoritative, which unconditionally
    # calls _sqlite_backend.get_repo(alias) -- configure it to mirror the
    # golden_repo Mock above so GoldenRepo(**repo_data) succeeds.
    manager._sqlite_backend.get_repo = Mock(
        return_value={
            "alias": golden_repo.alias,
            "repo_url": "https://example.com/test-repo.git",
            "default_branch": "main",
            "clone_path": golden_repo.clone_path,
            "created_at": "2026-01-01T00:00:00+00:00",
            "enable_temporal": golden_repo.enable_temporal,
            "temporal_options": golden_repo.temporal_options,
            "category_id": None,
            "category_auto_assigned": False,
        }
    )
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

    def test_semantic_command_receives_sanitized_non_none_env_in_postgres_mode(
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
                alias="test-repo", index_types=["semantic"]
            )
            _run_captured_worker(manager)

        by_phase = {c["phase_name"]: c["env"] for c in calls}
        semantic_env = by_phase["semantic"]
        assert semantic_env is not None, (
            "Bug #1325: the semantic Popen call must receive a sanitized env "
            "(build_cidx_subprocess_env), never raw None"
        )
        assert_env_absent(
            semantic_env,
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
            msg=(
                "the semantic Popen call must NEVER receive the temporal-only PG "
                "bootstrap var -- only the temporal call is postgres-aware for "
                "that var"
            ),
        )

    def test_temporal_command_receives_non_none_env_with_embedding_stats_var_in_sqlite_mode(
        self, tmp_path
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
