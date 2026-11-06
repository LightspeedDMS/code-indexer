"""EVO-66105: ActivatedRepoIndexManager threads repo_alias as
golden_repo_alias through to build_embedding_stats_child_env for
per-repo VoyageAI cost correlation.

The three execute methods (_execute_semantic_indexing,
_execute_fts_indexing, _execute_temporal_indexing) all converge on
_run_subprocess_with_telemetry, which merges the golden_repo_alias
into the child env via build_embedding_stats_child_env.  These tests
prove the env var CIDX_EMBEDDING_STATS_GOLDEN_REPO_ALIAS reaches the
subprocess for each indexing type.
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
from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
    EMBEDDING_STATS_GOLDEN_REPO_ALIAS_ENV,
)
from code_indexer.server.utils.config_manager import ServerConfig


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
    repo_path = str(
        Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
    )
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


class TestActivatedRepoIndexManagerEmbedStatsAlias:
    """EVO-66105: repo_alias threads through as CIDX_EMBEDDING_STATS_GOLDEN_REPO_ALIAS."""

    def test_semantic_indexing_threads_golden_repo_alias(
        self, index_manager, tmp_path, capturing_subprocess_run
    ):
        (tmp_path / ".code-indexer").mkdir()
        (tmp_path / ".code-indexer" / "config.json").write_text("{}")

        run_fn, captured_calls = capturing_subprocess_run

        with patch(
            "code_indexer.server.services.activated_repo_index_manager"
            ".run_cancellable_subprocess",
            side_effect=run_fn,
        ):
            index_manager._execute_semantic_indexing(
                str(tmp_path), clear=False, repo_alias="my-repo"
            )

        assert len(captured_calls) == 1
        env = captured_calls[0]["env"]
        assert env is not None
        assert env.get(EMBEDDING_STATS_GOLDEN_REPO_ALIAS_ENV) == "my-repo"

    def test_fts_indexing_threads_golden_repo_alias(
        self, index_manager, tmp_path, capturing_subprocess_run
    ):
        (tmp_path / ".code-indexer").mkdir()
        (tmp_path / ".code-indexer" / "config.json").write_text("{}")

        run_fn, captured_calls = capturing_subprocess_run

        with patch(
            "code_indexer.server.services.activated_repo_index_manager"
            ".run_cancellable_subprocess",
            side_effect=run_fn,
        ):
            index_manager._execute_fts_indexing(
                str(tmp_path), clear=False, repo_alias="my-repo"
            )

        assert len(captured_calls) == 1
        env = captured_calls[0]["env"]
        assert env is not None
        assert env.get(EMBEDDING_STATS_GOLDEN_REPO_ALIAS_ENV) == "my-repo"

    def test_temporal_indexing_threads_golden_repo_alias(
        self, index_manager, tmp_path, capturing_subprocess_run
    ):
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
                "code_indexer.server.services.activated_repo_index_manager"
                ".get_config_service"
            ) as mock_get_cfg_svc,
        ):
            mock_get_cfg_svc.return_value.get_config.return_value = server_config
            index_manager._execute_temporal_indexing(
                str(tmp_path), clear=False, repo_alias="my-repo"
            )

        assert len(captured_calls) == 1
        env = captured_calls[0]["env"]
        assert env is not None
        assert env.get(EMBEDDING_STATS_GOLDEN_REPO_ALIAS_ENV) == "my-repo"
