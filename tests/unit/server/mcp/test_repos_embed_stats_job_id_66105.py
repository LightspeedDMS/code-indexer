"""EVO-66105: _provider_index_job and _provider_temporal_index_job thread
job_id from kwargs through to build_embedding_stats_child_env so the
child subprocess env carries CIDX_EMBEDDING_STATS_JOB_ID.

Both functions converge on _run_provider_subprocess, which merges the
job_id into the env via build_embedding_stats_child_env.  These tests
capture the env passed to the actual subprocess runner and assert the
job_id env var is present.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
    EMBEDDING_STATS_GOLDEN_REPO_ALIAS_ENV,
    EMBEDDING_STATS_JOB_ID_ENV,
)
from code_indexer.server.utils.config_manager import ServerConfig

_MODULE = "code_indexer.server.mcp.handlers.repos"


@pytest.fixture
def server_config():
    return ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")


@pytest.fixture
def capturing_popen_progress():
    """Capture the env kwarg passed to run_with_popen_progress."""
    captured_calls: list = []

    def _run(*, env=None, **kwargs):
        captured_calls.append({"env": env})

    return _run, captured_calls


class TestProviderIndexJobThreadsJobId:
    def test_provider_index_job_threads_job_id_to_child_env(
        self, tmp_path, server_config, capturing_popen_progress
    ):
        repo_path = str(tmp_path)
        run_fn, captured_calls = capturing_popen_progress

        with (
            patch(
                f"{_MODULE}._resolve_versioned_to_base_clone",
                return_value=(repo_path, "test-alias", False, None),
            ),
            patch(f"{_MODULE}._resolve_provider_api_key", return_value=None),
            patch(f"{_MODULE}.get_config_service") as mock_cfg,
            patch(
                "code_indexer.server.services.config_seeding.seed_provider_config",
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=run_fn,
            ),
            patch(
                "code_indexer.services.provider_health_bridge.drain_and_feed_monitor",
            ),
        ):
            mock_cfg.return_value.get_config.return_value = server_config

            from code_indexer.server.mcp.handlers.repos import _provider_index_job

            _provider_index_job(
                repo_path,
                "voyage-ai",
                clear=False,
                progress_callback=None,
                repo_alias="test-alias",
                job_id="test-job-123",
            )

        assert len(captured_calls) == 1
        env = captured_calls[0]["env"]
        assert env is not None
        assert env.get(EMBEDDING_STATS_JOB_ID_ENV) == "test-job-123"
        assert env.get(EMBEDDING_STATS_GOLDEN_REPO_ALIAS_ENV) == "test-alias"


class TestProviderTemporalIndexJobThreadsJobId:
    def test_provider_temporal_index_job_threads_job_id_to_child_env(
        self, tmp_path, server_config, capturing_popen_progress
    ):
        repo_path = str(tmp_path)
        run_fn, captured_calls = capturing_popen_progress

        with (
            patch(
                f"{_MODULE}._resolve_provider_job_repo_path",
                return_value=(repo_path, "test-alias", False),
            ),
            patch(
                f"{_MODULE}._build_provider_api_key_env",
                return_value=dict(__import__("os").environ),
            ),
            patch(f"{_MODULE}.get_config_service") as mock_cfg,
            patch(
                "code_indexer.server.storage.postgres.temporal_child_wiring"
                ".build_temporal_child_env",
                return_value=None,
            ),
            patch(
                "code_indexer.server.services.config_seeding.seed_provider_config",
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=run_fn,
            ),
            patch(
                "code_indexer.services.provider_health_bridge.drain_and_feed_monitor",
            ),
        ):
            mock_cfg.return_value.get_config.return_value = server_config

            from code_indexer.server.mcp.handlers.repos import (
                _provider_temporal_index_job,
            )

            _provider_temporal_index_job(
                repo_path,
                "voyage-ai",
                clear=False,
                progress_callback=None,
                repo_alias="test-alias",
                job_id="test-job-456",
            )

        assert len(captured_calls) == 1
        env = captured_calls[0]["env"]
        assert env is not None
        assert env.get(EMBEDDING_STATS_JOB_ID_ENV) == "test-job-456"
        assert env.get(EMBEDDING_STATS_GOLDEN_REPO_ALIAS_ENV) == "test-alias"
