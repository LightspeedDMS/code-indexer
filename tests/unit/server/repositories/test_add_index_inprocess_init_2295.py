from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from code_indexer.services.progress_subprocess_runner import IndexingSubprocessError
from tests.unit.server.repositories.test_add_golden_repo_index_error_messages import (
    _extract_background_worker,
    _make_manager_for_add_index,
)
from tests.utils.real_process_spawn_recorder import record_cidx_spawns

_RUNNER = "code_indexer.services.progress_subprocess_runner.run_with_popen_progress"
_STOP = "indexing stopped by test"
_INIT = "code_indexer.services.local_project_init.initialize_local_project"


def _worker_for(repo: Path):
    manager, golden_repo_error = _make_manager_for_add_index()
    manager.get_actual_repo_path = Mock(return_value=str(repo))
    return _extract_background_worker(manager, "semantic"), golden_repo_error


def _run_until_indexing(worker, golden_repo_error) -> Mock:
    runner = Mock(side_effect=IndexingSubprocessError(_STOP))
    with patch(_RUNNER, runner):
        with pytest.raises(golden_repo_error) as exc_info:
            worker()
    assert _STOP in str(exc_info.value)
    return runner


def test_fresh_repo_is_initialized_in_process_before_indexing(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    worker, golden_repo_error = _worker_for(repo)
    spawns = record_cidx_spawns(tmp_path, monkeypatch)

    runner = _run_until_indexing(worker, golden_repo_error)

    assert spawns() == []
    config = json.loads((repo / ".code-indexer" / "config.json").read_text())
    assert config["codebase_dir"] == str(repo.resolve())
    assert (repo / ".code-indexer" / "language-mappings.yaml").is_file()
    assert not (repo / ".code-indexer-override.yaml").exists()
    runner.assert_called_once()


def test_existing_valid_config_is_left_alone(tmp_path, monkeypatch):
    from code_indexer.services.local_project_init import initialize_local_project

    repo = tmp_path / "repo"
    repo.mkdir()
    initialize_local_project(repo.resolve(), no_override_file=True)
    config_path = repo / ".code-indexer" / "config.json"
    before = config_path.read_bytes()
    worker, golden_repo_error = _worker_for(repo)
    spawns = record_cidx_spawns(tmp_path, monkeypatch)

    _run_until_indexing(worker, golden_repo_error)

    assert spawns() == []
    assert config_path.read_bytes() == before


def test_corrupt_config_fails_before_indexing(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".code-indexer").mkdir(parents=True)
    (repo / ".code-indexer" / "config.json").write_text("{not json")
    worker, golden_repo_error = _worker_for(repo)
    spawns = record_cidx_spawns(tmp_path, monkeypatch)
    runner = Mock()

    with patch(_RUNNER, runner):
        with pytest.raises(golden_repo_error) as exc_info:
            worker()

    message = str(exc_info.value)
    assert "Failed to initialize repo before indexing" in message
    assert "ConfigCorruptionError" in message
    assert "Expecting property name" in message
    assert spawns() == []
    runner.assert_not_called()


def test_causeless_init_failure_reports_the_exception_type(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    worker, golden_repo_error = _worker_for(repo)
    runner = Mock()

    with patch(_INIT, side_effect=OSError()), patch(_RUNNER, runner):
        with pytest.raises(golden_repo_error) as exc_info:
            worker()

    assert str(exc_info.value) == "Failed to initialize repo before indexing: OSError"
    runner.assert_not_called()
