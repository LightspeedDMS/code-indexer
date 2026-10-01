"""AC12: Activated-repo reindex rejects explicit temporal requests (Story #1457).

Temporal data is owned exclusively by the golden repo's shared sister
location (AC1-AC11) and must never be built locally for an activated
(non-golden) repo. `ActivatedRepoIndexManager` is exclusively used for
activated repos (golden repos go through `GoldenRepoManager` instead), so
any `trigger_reindex` call through it requesting `index_types` including
"temporal" must be REJECTED with a loud, explicit error -- never a silent
no-op, and never allowed to actually build local temporal data (which is
the CONFIRMED current behavior via `_execute_single_index_type`'s
"temporal" -> `_execute_temporal_indexing` dispatch).
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


@patch("os.path.exists")
def test_trigger_reindex_rejects_temporal_index_type(mock_exists, index_manager):
    mock_exists.return_value = True

    with pytest.raises(ValueError, match="temporal"):
        index_manager.trigger_reindex(
            repo_alias="test-repo",
            index_types=["temporal"],
            clear=False,
            username="testuser",
        )

    # Never silently dropped/no-op'd: no background job submitted.
    index_manager.background_job_manager.submit_job.assert_not_called()
