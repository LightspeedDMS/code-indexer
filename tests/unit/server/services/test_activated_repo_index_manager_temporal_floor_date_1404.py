"""Tests for Story #1404 launch-site wiring in
activated_repo_index_manager.py::_execute_temporal_indexing (launch site 3
of the 4 corrected sites).

This site has NO per-repo since_date concept (activated repos carry no
temporal_options) -- only the global floor date applies here, so no
precedence composition is needed: the resolved global floor date is passed
straight through as --since-date, omitted entirely when unset (Scenario 5
no-op).

Mirrors test_activated_repo_index_manager_temporal_pg_env_wiring_1313.py's
exact mocking pattern, capturing the `args` list instead of `env`.
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
from code_indexer.server.utils.config_manager import (
    ServerConfig,
    TemporalIndexingConfig,
)


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
    repo_path = str(Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo")
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
    captured_calls: list = []

    def _run(args, env=None, **kwargs):
        captured_calls.append({"args": args, "env": env})
        return Mock(returncode=0, stdout="", stderr="")

    return _run, captured_calls


def _server_config(floor_date):
    return ServerConfig(
        server_dir="/opt/cidx-server",
        temporal_indexing_config=TemporalIndexingConfig(index_floor_date=floor_date),
    )


class TestExecuteTemporalIndexingFloorDateWiring:
    def test_configured_floor_date_appears_in_args(
        self, index_manager, tmp_path, capturing_subprocess_run
    ) -> None:
        run_fn, captured_calls = capturing_subprocess_run

        with (
            patch(
                "code_indexer.server.services.activated_repo_index_manager"
                ".run_cancellable_subprocess",
                side_effect=run_fn,
            ),
            patch(
                "code_indexer.server.services.activated_repo_index_manager.get_config_service"
            ) as mock_get_cfg_svc,
            patch(
                "code_indexer.server.services.config_service.get_config_service",
                new=mock_get_cfg_svc,
            ),
        ):
            mock_get_cfg_svc.return_value.get_config.return_value = _server_config(
                "2025-01-01"
            )
            index_manager._execute_temporal_indexing(str(tmp_path), clear=False)

        assert len(captured_calls) == 1
        args = captured_calls[0]["args"]
        assert "--since-date" in args, f"Expected --since-date in args. Got: {args}"
        idx = args.index("--since-date")
        assert args[idx + 1] == "2025-01-01"

    def test_unset_floor_date_omits_flag(
        self, index_manager, tmp_path, capturing_subprocess_run
    ) -> None:
        """Scenario 5: unset floor = full-history no-op."""
        run_fn, captured_calls = capturing_subprocess_run

        with (
            patch(
                "code_indexer.server.services.activated_repo_index_manager"
                ".run_cancellable_subprocess",
                side_effect=run_fn,
            ),
            patch(
                "code_indexer.server.services.activated_repo_index_manager.get_config_service"
            ) as mock_get_cfg_svc,
            patch(
                "code_indexer.server.services.config_service.get_config_service",
                new=mock_get_cfg_svc,
            ),
        ):
            mock_get_cfg_svc.return_value.get_config.return_value = _server_config(None)
            index_manager._execute_temporal_indexing(str(tmp_path), clear=False)

        assert len(captured_calls) == 1
        args = captured_calls[0]["args"]
        assert "--since-date" not in args, f"Expected no --since-date. Got: {args}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
