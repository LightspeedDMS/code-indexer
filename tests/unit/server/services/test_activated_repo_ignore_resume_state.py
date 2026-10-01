"""Indexer resume-state trust and containment.

Server-spawned indexing (activated-repo reindex) must
not trust repository-authored resume state
(.code-indexer/metadata-<provider>.json lives inside the activated repo's
CoW clone -- writable by the tenant who activated it, or a committer whose
content later flowed into it via git pull/sync). The chosen mechanism is
the new `cidx index --ignore-resume-state` CLI flag (threads
SmartIndexer.smart_index(trust_resume_state=False)) rather than `--clear`
(which would force a full re-embed on every reindex -- prohibitive at
~900-repo production scale, and `_execute_semantic_indexing` never even
appended `--clear` to its subprocess command to begin with, relying
instead on a server-side directory delete that leaves the
committer-writable metadata file untouched).

These tests exercise the REAL `_execute_semantic_indexing` and
`_execute_fts_indexing` methods, capturing the actual subprocess argv via
the same mocking pattern as
tests/unit/server/services/test_activated_repo_index_manager_subprocess_env_sanitization_1325.py,
and assert the command includes --ignore-resume-state.
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


@pytest.fixture
def capturing_subprocess_run():
    captured_calls: list = []

    def _run(args, env=None, **kwargs):
        captured_calls.append({"args": args, "env": env})
        return Mock(returncode=0, stdout="", stderr="")

    return _run, captured_calls


class TestActivatedRepoIndexManagerIgnoresResumeState:
    def test_semantic_indexing_command_ignores_resume_state(
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
            index_manager._execute_semantic_indexing(str(tmp_path), clear=False)

        assert len(captured_calls) == 1
        assert "--ignore-resume-state" in captured_calls[0]["args"], (
            "SECURITY: activated-repo semantic reindex must never trust "
            f"tenant-authored resume state. Got args: {captured_calls[0]['args']}"
        )

    def test_fts_indexing_command_ignores_resume_state(
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
            index_manager._execute_fts_indexing(str(tmp_path), clear=False)

        assert len(captured_calls) == 1
        assert "--ignore-resume-state" in captured_calls[0]["args"], (
            "SECURITY: activated-repo FTS reindex must never trust "
            f"tenant-authored resume state. Got args: {captured_calls[0]['args']}"
        )
