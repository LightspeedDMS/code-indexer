"""Bug #1575 Part C remediation (independent re-review): the scheduled
refresh path (`RefreshScheduler._index_source`) is THE dominant, steady-
state production `cidx index` spawn site -- it runs on every golden repo,
every refresh cycle -- and never set CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV
at all. Unlike `_execute_post_clone_workflow`'s semantic/temporal Popen
calls (already fixed in golden_repo_manager.py), this spawn site silently
inherited the unsafe enabled-by-default fallback even in postgres/cluster
mode.

Mirrors test_refresh_scheduler_temporal_pg_env_wiring_1313.py's fixtures
and calling convention, targeting the same `RefreshScheduler._index_source`
call site. Parameterized over (phase, storage_mode) to cover both the
semantic and temporal spawns without duplicating the setup/assert blocks.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.config import ConfigManager
from code_indexer.server.utils.config_manager import ServerConfig
from code_indexer.storage.shared.hnsw_sync_state import (
    CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
)
from tests.utils.env_assertions import assert_env_absent, assert_env_value

_FAKE_POSTGRES_DSN = "postgresql://not-a-real-credential@example.invalid/placeholder"


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
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        registry=mock_registry,
    )


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


def _run_index_source_and_capture(scheduler, source_repo, server_config):
    calls: list = []
    with (
        patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=_make_fake_run_with_popen_progress(calls),
        ),
        patch(
            "code_indexer.global_repos.refresh_scheduler.get_config_service"
        ) as mock_get_cfg_svc,
        # resolve_hnsw_sync_epoch_env_var() performs its OWN independent
        # deferred import of get_config_service from its canonical module
        # path (by design -- see its docstring), so refresh_scheduler's
        # own name-imported reference above does not affect it; this
        # second patch target is required for the resolver to see the
        # same fake ServerConfig.
        patch(
            "code_indexer.server.services.config_service.get_config_service"
        ) as mock_get_cfg_svc_for_resolver,
    ):
        mock_get_cfg_svc.return_value.get_config.return_value = server_config
        mock_get_cfg_svc_for_resolver.return_value.get_config.return_value = (
            server_config
        )

        scheduler._index_source(
            alias_name="test-repo-global",
            source_path=str(source_repo),
        )

    return {c["phase_name"]: c["env"] for c in calls}


_POSTGRES_CONFIG = ServerConfig(
    server_dir="/opt/cidx-server",
    storage_mode="postgres",
    postgres_dsn=_FAKE_POSTGRES_DSN,
)
_SQLITE_CONFIG = ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")


@pytest.mark.parametrize("phase", ["semantic", "temporal"])
@pytest.mark.parametrize(
    "server_config, expected_var",
    [
        pytest.param(_POSTGRES_CONFIG, "1", id="postgres"),
        pytest.param(_SQLITE_CONFIG, None, id="sqlite"),
    ],
)
def test_hnsw_epoch_env_matches_storage_mode(
    scheduler, source_repo, phase, server_config, expected_var
):
    """The scheduled-refresh {semantic,temporal} `cidx index` child must
    receive CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV="1" in postgres/cluster
    mode, and must never receive it in sqlite/solo mode. For temporal, this
    also proves the var survives being merged on top of the separate
    build_temporal_child_env() dict rather than being clobbered by it.
    """
    by_phase = _run_index_source_and_capture(scheduler, source_repo, server_config)

    env = by_phase[phase]
    if expected_var is None:
        assert_env_absent(
            env,
            CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
            msg=(
                f"sqlite/solo mode must never set this var on the {phase} "
                "child -- it defaults enabled"
            ),
        )
    else:
        assert_env_value(
            env,
            CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
            expected_var,
            msg=(
                f"the scheduled-refresh {phase} child must be told it is "
                "running in postgres/cluster mode"
            ),
        )
