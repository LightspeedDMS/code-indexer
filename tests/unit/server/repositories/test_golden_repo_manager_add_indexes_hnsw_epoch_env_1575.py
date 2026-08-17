"""Bug #1575 Part C remediation (independent re-review): the
`add_indexes_to_golden_repo` background worker spawns THREE `cidx index`
children (semantic, fts-rebuild, temporal) that never set
CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV at all -- unlike
`_execute_post_clone_workflow`'s semantic/temporal Popen calls (already
fixed), these spawn sites inherited the unsafe enabled-by-default fallback
even in postgres/cluster mode.

Note: `postgres_dsn` below is a non-functional placeholder string used only
to satisfy ServerConfig's dataclass shape in these fully-mocked unit tests
-- no real database is ever contacted.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.utils.config_manager import ServerConfig
from code_indexer.storage.shared.hnsw_sync_state import (
    CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
)
from tests.utils.env_assertions import assert_env_absent, assert_env_value

_FAKE_POSTGRES_DSN = "postgresql://not-a-real-credential@example.invalid/placeholder"


def _make_manager(tmp_path):
    """Build a GoldenRepoManager via its real constructor, then override
    only the external collaborators this test needs to control."""
    manager = GoldenRepoManager(data_dir=str(tmp_path))

    repo_path = tmp_path / "golden-repos" / "test-repo"
    (repo_path / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)

    golden_repo = Mock()
    golden_repo.alias = "test-repo"
    golden_repo.clone_path = str(repo_path)
    golden_repo.temporal_options = {}
    golden_repo.enable_temporal = False

    manager.golden_repos = {"test-repo": golden_repo}
    manager.get_actual_repo_path = Mock(return_value=str(repo_path))
    manager._sqlite_backend = Mock()
    manager._sqlite_backend.update_enable_temporal = Mock(return_value=True)
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


def _make_capturing_subprocess_run(captured_fts_env: dict):
    def _run(command, **kwargs):
        if "--rebuild-fts-index" in command:
            captured_fts_env["env"] = kwargs.get("env")
        return Mock(returncode=0, stdout="", stderr="")

    return _run


def _run_add_indexes_and_capture(
    manager, index_types, server_config, captured_fts_env
):
    calls = []

    def _fake_run_with_popen_progress(*, command, phase_name, env=None, **kwargs):
        calls.append({"phase_name": phase_name, "env": env})
        return 100

    with (
        patch(
            "code_indexer.server.repositories.golden_repo_manager.subprocess.run",
            side_effect=_make_capturing_subprocess_run(captured_fts_env),
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

        manager.add_indexes_to_golden_repo(alias="test-repo", index_types=index_types)
        _run_captured_worker(manager)

    return {c["phase_name"]: c["env"] for c in calls}


class TestAddIndexesSemanticGetsHnswEpochPostgresEnv:
    def test_receives_postgres_mode_var_when_cluster(self, tmp_path):
        manager, _ = _make_manager(tmp_path)
        server_config = ServerConfig(
            server_dir="/opt/cidx-server",
            storage_mode="postgres",
            postgres_dsn=_FAKE_POSTGRES_DSN,
        )
        by_phase = _run_add_indexes_and_capture(
            manager, ["semantic"], server_config, {}
        )
        assert_env_value(
            by_phase["semantic"],
            CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
            "1",
        )

    def test_omits_postgres_mode_var_when_sqlite(self, tmp_path):
        manager, _ = _make_manager(tmp_path)
        server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")
        by_phase = _run_add_indexes_and_capture(
            manager, ["semantic"], server_config, {}
        )
        assert_env_absent(
            by_phase["semantic"], CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV
        )


class TestAddIndexesTemporalGetsHnswEpochPostgresEnv:
    def test_receives_postgres_mode_var_when_cluster(self, tmp_path):
        manager, _ = _make_manager(tmp_path)
        server_config = ServerConfig(
            server_dir="/opt/cidx-server",
            storage_mode="postgres",
            postgres_dsn=_FAKE_POSTGRES_DSN,
        )
        by_phase = _run_add_indexes_and_capture(
            manager, ["temporal"], server_config, {}
        )
        assert_env_value(
            by_phase["temporal"],
            CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
            "1",
        )

    def test_omits_postgres_mode_var_when_sqlite(self, tmp_path):
        manager, _ = _make_manager(tmp_path)
        server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")
        by_phase = _run_add_indexes_and_capture(
            manager, ["temporal"], server_config, {}
        )
        assert_env_absent(
            by_phase["temporal"], CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV
        )


class TestAddIndexesFtsRebuildGetsHnswEpochPostgresEnv:
    def test_receives_postgres_mode_var_when_cluster(self, tmp_path):
        manager, _ = _make_manager(tmp_path)
        server_config = ServerConfig(
            server_dir="/opt/cidx-server",
            storage_mode="postgres",
            postgres_dsn=_FAKE_POSTGRES_DSN,
        )
        captured_fts_env: dict = {}
        _run_add_indexes_and_capture(manager, ["fts"], server_config, captured_fts_env)
        assert captured_fts_env.get("env") is not None
        assert_env_value(
            captured_fts_env["env"],
            CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
            "1",
        )

    def test_omits_postgres_mode_var_when_sqlite(self, tmp_path):
        manager, _ = _make_manager(tmp_path)
        server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")
        captured_fts_env: dict = {}
        _run_add_indexes_and_capture(manager, ["fts"], server_config, captured_fts_env)
        assert captured_fts_env.get("env") is not None
        assert_env_absent(
            captured_fts_env["env"], CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV
        )
