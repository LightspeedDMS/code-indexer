"""
Unit tests for add_golden_repo_index subprocess error message fallback chain.

Bug #361: When cidx subprocess calls fail, error messages are empty because
result.stderr is empty — the actual error is in result.stdout.

Verifies the fallback chain:
1. result.stderr (primary — existing behavior preserved)
2. result.stdout (fallback when stderr is empty)
3. f"Exit code {result.returncode}" (last resort when both are empty)

Affected locations in golden_repo_manager.py:
- cidx index (semantic) failure (line ~2411)
- cidx index --rebuild-fts-index (FTS) failure (line ~2427)
- cidx index --index-commits (temporal) failure (line ~2460)
- cidx scip generate (SCIP) failure (line ~2529)
"""

from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

_REPO_PATH = "/fake/data/golden-repos/test-repo"


@pytest.fixture(autouse=True)
def _initialized_repo(tmp_path, monkeypatch):
    from code_indexer.services.local_project_init import initialize_local_project

    repo = tmp_path / "test-repo"
    repo.mkdir()
    initialize_local_project(repo.resolve(), no_override_file=True)
    monkeypatch.setattr(f"{__name__}._REPO_PATH", str(repo))
    return Path(repo)


def _make_subprocess_result(returncode, stdout="", stderr=""):
    """Create a mock subprocess.CompletedProcess result."""
    result = Mock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _make_popen_mock(returncode, stderr="", stdout_lines=None):
    """Create a mock subprocess.Popen process for run_with_popen_progress.

    run_with_popen_progress uses:
    - process.stdout (iterable of lines)
    - process.stderr.readlines() (drained in a background thread)
    - process.wait()
    - process.returncode
    """
    mock_process = MagicMock()
    mock_process.stdout = iter(stdout_lines or [])
    mock_process.stderr.readlines.return_value = [stderr] if stderr else []
    mock_process.returncode = returncode
    mock_process.wait.return_value = None
    return mock_process


def _make_manager_for_add_index():
    """
    Instantiate a GoldenRepoManager bypassing __init__ and configure
    the minimum attributes needed to invoke the background_worker closure
    returned by add_golden_repo_index.
    """
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoManager,
        GoldenRepoError,
    )

    with patch.object(GoldenRepoManager, "__init__", lambda self, *a, **kw: None):
        manager = GoldenRepoManager.__new__(GoldenRepoManager)

    # Minimal attributes required by add_golden_repo_index body
    manager.data_dir = "/fake/data"
    manager.golden_repos_dir = "/fake/data/golden-repos"

    # Create a fake golden repo
    golden_repo = Mock()
    golden_repo.alias = "test-repo"
    golden_repo.clone_path = "/fake/data/golden-repos/test-repo"
    golden_repo.temporal_options = {}
    golden_repo.enable_temporal = False

    manager.golden_repos = {"test-repo": golden_repo}
    manager.get_actual_repo_path = Mock(return_value=_REPO_PATH)

    # Bug #1316: add_indexes_to_golden_repo's background_worker now resolves
    # `repo` authoritatively via _resolve_golden_repo_authoritative, which
    # unconditionally calls _sqlite_backend.get_repo(alias) -- configure it
    # to mirror the golden_repo Mock above so GoldenRepo(**repo_data) succeeds.
    manager._sqlite_backend = Mock()
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

    # background_job_manager: capture submitted func so we can call it directly
    manager.background_job_manager = Mock()
    manager.background_job_manager.submit_job = Mock(return_value="job-123")

    return manager, GoldenRepoError


def _extract_background_worker(manager, index_type):
    """
    Call add_golden_repo_index and return the background_worker closure
    that was passed to background_job_manager.submit_job.
    """
    manager.add_index_to_golden_repo(
        alias="test-repo",
        index_type=index_type,
        submitter_username="admin",
    )
    call_kwargs = manager.background_job_manager.submit_job.call_args
    # func is a keyword arg
    worker = call_kwargs.kwargs.get("func") or call_kwargs[1].get("func")
    return worker


# ---------------------------------------------------------------------------
# cidx index (semantic) failure
# ---------------------------------------------------------------------------


class TestSemanticIndexErrorMessages:
    """Tests for cidx index (semantic) failure error message fallback chain.

    Semantic indexing uses subprocess.Popen (via run_with_popen_progress) for
    real-time progress streaming; only that Popen is mocked.
    """

    def _run_semantic_worker_with_result(self, semantic_result):
        """Helper: run semantic worker with a controlled index result."""
        manager, GoldenRepoError = _make_manager_for_add_index()
        worker = _extract_background_worker(manager, "semantic")

        mock_popen = _make_popen_mock(
            returncode=semantic_result.returncode,
            stderr=semantic_result.stderr,
            stdout_lines=[semantic_result.stdout] if semantic_result.stdout else [],
        )

        with patch("subprocess.Popen", return_value=mock_popen):
            with pytest.raises(GoldenRepoError) as exc_info:
                worker()

        return exc_info

    def test_semantic_stderr_used_when_present(self):
        """When cidx index (semantic) fails with stderr content, error uses stderr."""
        sem_fail = _make_subprocess_result(
            1, stdout="", stderr="VoyageAI API key invalid"
        )
        exc_info = self._run_semantic_worker_with_result(sem_fail)
        assert "VoyageAI API key invalid" in str(exc_info.value)

    def test_semantic_stdout_used_when_stderr_empty(self):
        """When cidx index (semantic) fails with empty stderr, error falls back to stdout."""
        sem_fail = _make_subprocess_result(
            1,
            stdout="Fatal: embedding provider not configured\n",
            stderr="",
        )
        exc_info = self._run_semantic_worker_with_result(sem_fail)
        error_msg = str(exc_info.value)
        assert "Fatal: embedding provider not configured" in error_msg
        assert error_msg.rstrip() != "Failed to create semantic index:"

    def test_semantic_exit_code_used_when_both_empty(self):
        """When cidx index (semantic) fails with both outputs empty, uses exit code."""
        sem_fail = _make_subprocess_result(99, stdout="", stderr="")
        exc_info = self._run_semantic_worker_with_result(sem_fail)
        assert "99" in str(exc_info.value)


# ---------------------------------------------------------------------------
# cidx index --rebuild-fts-index (FTS) failure
# ---------------------------------------------------------------------------


class TestFTSIndexErrorMessages:
    """Tests for cidx index --rebuild-fts-index (FTS) failure error message fallback."""

    def _run_fts_worker_with_result(self, fts_result):
        manager, GoldenRepoError = _make_manager_for_add_index()
        worker = _extract_background_worker(manager, "fts")

        with patch("subprocess.run", return_value=fts_result):
            with pytest.raises(GoldenRepoError) as exc_info:
                worker()

        return exc_info

    def test_fts_stderr_used_when_present(self):
        """When FTS index fails with stderr content, error uses stderr."""
        fts_fail = _make_subprocess_result(1, stdout="", stderr="Tantivy schema error")
        exc_info = self._run_fts_worker_with_result(fts_fail)
        assert "Tantivy schema error" in str(exc_info.value)

    def test_fts_stdout_used_when_stderr_empty(self):
        """When FTS index fails with empty stderr, error falls back to stdout."""
        fts_fail = _make_subprocess_result(
            1, stdout="Error: FTS directory already locked\n", stderr=""
        )
        exc_info = self._run_fts_worker_with_result(fts_fail)
        error_msg = str(exc_info.value)
        assert "Error: FTS directory already locked" in error_msg
        assert error_msg.rstrip() != "Failed to create FTS index:"

    def test_fts_exit_code_used_when_both_empty(self):
        """When FTS index fails with both outputs empty, uses exit code."""
        fts_fail = _make_subprocess_result(7, stdout="", stderr="")
        exc_info = self._run_fts_worker_with_result(fts_fail)
        assert "7" in str(exc_info.value)


# ---------------------------------------------------------------------------
# cidx index --index-commits (temporal) failure
# ---------------------------------------------------------------------------


class TestTemporalIndexErrorMessages:
    """Tests for cidx index --index-commits (temporal) failure error message fallback.

    Temporal indexing uses subprocess.Popen (via run_with_popen_progress) for
    real-time progress streaming; only that Popen is mocked.
    """

    def _run_temporal_worker_with_result(self, temporal_result):
        manager, GoldenRepoError = _make_manager_for_add_index()
        worker = _extract_background_worker(manager, "temporal")

        mock_popen = _make_popen_mock(
            returncode=temporal_result.returncode,
            stderr=temporal_result.stderr,
            stdout_lines=[temporal_result.stdout] if temporal_result.stdout else [],
        )

        with patch("subprocess.Popen", return_value=mock_popen):
            with pytest.raises(GoldenRepoError) as exc_info:
                worker()

        return exc_info

    def test_temporal_stderr_used_when_present(self):
        """When temporal index fails with stderr content, error uses stderr."""
        temp_fail = _make_subprocess_result(
            1, stdout="", stderr="git: not a git repository"
        )
        exc_info = self._run_temporal_worker_with_result(temp_fail)
        assert "git: not a git repository" in str(exc_info.value)

    def test_temporal_stdout_used_when_stderr_empty(self):
        """When temporal index fails with empty stderr, error falls back to stdout."""
        temp_fail = _make_subprocess_result(
            1, stdout="Error: no commits found in repository\n", stderr=""
        )
        exc_info = self._run_temporal_worker_with_result(temp_fail)
        error_msg = str(exc_info.value)
        assert "Error: no commits found in repository" in error_msg
        assert error_msg.rstrip() != "Failed to create temporal index:"

    def test_temporal_exit_code_used_when_both_empty(self):
        """When temporal index fails with both outputs empty, uses exit code."""
        temp_fail = _make_subprocess_result(3, stdout="", stderr="")
        exc_info = self._run_temporal_worker_with_result(temp_fail)
        assert "3" in str(exc_info.value)


# ---------------------------------------------------------------------------
# cidx scip generate (SCIP) failure
# ---------------------------------------------------------------------------


class TestSCIPIndexErrorMessages:
    """Tests for cidx scip generate (SCIP) failure error message fallback chain."""

    def _run_scip_worker_with_result(self, scip_result):
        manager, GoldenRepoError = _make_manager_for_add_index()
        worker = _extract_background_worker(manager, "scip")

        with patch("subprocess.run", return_value=scip_result):
            with pytest.raises(GoldenRepoError) as exc_info:
                worker()

        return exc_info

    def test_scip_stderr_used_when_present(self):
        """When SCIP generation fails with stderr content, error uses stderr."""
        scip_fail = _make_subprocess_result(
            1, stdout="", stderr="scip-python: command not found"
        )
        exc_info = self._run_scip_worker_with_result(scip_fail)
        assert "scip-python: command not found" in str(exc_info.value)

    def test_scip_stdout_used_when_stderr_empty(self):
        """When SCIP generation fails with empty stderr, error falls back to stdout."""
        scip_fail = _make_subprocess_result(
            1, stdout="Error: unsupported language for SCIP indexing\n", stderr=""
        )
        exc_info = self._run_scip_worker_with_result(scip_fail)
        error_msg = str(exc_info.value)
        assert "Error: unsupported language for SCIP indexing" in error_msg
        assert error_msg.rstrip() != "Failed to create SCIP index:"

    def test_scip_exit_code_used_when_both_empty(self):
        """When SCIP generation fails with both outputs empty, uses exit code."""
        scip_fail = _make_subprocess_result(5, stdout="", stderr="")
        exc_info = self._run_scip_worker_with_result(scip_fail)
        assert "5" in str(exc_info.value)
