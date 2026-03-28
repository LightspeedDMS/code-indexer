"""
Unit tests for add_golden_repo_index subprocess error message fallback chain.

Bug #361: When cidx subprocess calls fail, error messages are empty because
result.stderr is empty — the actual error is in result.stdout.

Verifies the fallback chain:
1. result.stderr (primary — existing behavior preserved)
2. result.stdout (fallback when stderr is empty)
3. f"Exit code {result.returncode}" (last resort when both are empty)

Affected locations in golden_repo_manager.py:
- cidx init failure (line ~2391)
- cidx index (semantic) failure (line ~2411)
- cidx index --rebuild-fts-index (FTS) failure (line ~2427)
- cidx index --index-commits (temporal) failure (line ~2460)
- cidx scip generate (SCIP) failure (line ~2529)
"""

from unittest.mock import Mock, patch

import pytest


def _make_subprocess_result(returncode, stdout="", stderr=""):
    """Create a mock subprocess.CompletedProcess result."""
    result = Mock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


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
    manager.get_actual_repo_path = Mock(
        return_value="/fake/data/golden-repos/test-repo"
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
# cidx init failure
# ---------------------------------------------------------------------------


class TestInitSubprocessErrorMessages:
    """Tests for cidx init failure error message fallback chain."""

    def test_init_stderr_used_when_present(self):
        """When cidx init fails and stderr has content, error uses stderr."""
        manager, GoldenRepoError = _make_manager_for_add_index()
        worker = _extract_background_worker(manager, "semantic")

        init_fail = _make_subprocess_result(
            returncode=1,
            stdout="some stdout text",
            stderr="meaningful stderr error",
        )
        with patch("subprocess.run", return_value=init_fail):
            with pytest.raises(GoldenRepoError) as exc_info:
                worker()

        assert "meaningful stderr error" in str(exc_info.value)

    def test_init_stdout_used_when_stderr_empty(self):
        """When cidx init fails with empty stderr, error falls back to stdout."""
        manager, GoldenRepoError = _make_manager_for_add_index()
        worker = _extract_background_worker(manager, "semantic")

        init_fail = _make_subprocess_result(
            returncode=1,
            stdout="Error: config file not found\n",
            stderr="",
        )
        with patch("subprocess.run", return_value=init_fail):
            with pytest.raises(GoldenRepoError) as exc_info:
                worker()

        error_msg = str(exc_info.value)
        assert "Error: config file not found" in error_msg
        # Should NOT just show empty string or bare colon
        assert error_msg.rstrip() != "Failed to initialize repo before indexing:"

    def test_init_exit_code_used_when_both_empty(self):
        """When cidx init fails with both stderr and stdout empty, uses exit code."""
        manager, GoldenRepoError = _make_manager_for_add_index()
        worker = _extract_background_worker(manager, "semantic")

        init_fail = _make_subprocess_result(
            returncode=42,
            stdout="",
            stderr="",
        )
        with patch("subprocess.run", return_value=init_fail):
            with pytest.raises(GoldenRepoError) as exc_info:
                worker()

        assert "42" in str(exc_info.value)


# ---------------------------------------------------------------------------
# cidx index (semantic) failure
# ---------------------------------------------------------------------------


class TestSemanticIndexErrorMessages:
    """Tests for cidx index (semantic) failure error message fallback chain."""

    def _run_semantic_worker_with_results(self, init_result, semantic_result):
        """Helper: run semantic worker with controlled subprocess results."""
        manager, GoldenRepoError = _make_manager_for_add_index()
        worker = _extract_background_worker(manager, "semantic")

        call_count = [0]

        def side_effect(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return init_result
            return semantic_result

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(GoldenRepoError) as exc_info:
                worker()

        return exc_info

    def test_semantic_stderr_used_when_present(self):
        """When cidx index (semantic) fails with stderr content, error uses stderr."""
        init_ok = _make_subprocess_result(0, stdout="initialized", stderr="")
        sem_fail = _make_subprocess_result(
            1, stdout="", stderr="VoyageAI API key invalid"
        )
        exc_info = self._run_semantic_worker_with_results(init_ok, sem_fail)
        assert "VoyageAI API key invalid" in str(exc_info.value)

    def test_semantic_stdout_used_when_stderr_empty(self):
        """When cidx index (semantic) fails with empty stderr, error falls back to stdout."""
        init_ok = _make_subprocess_result(0, stdout="initialized", stderr="")
        sem_fail = _make_subprocess_result(
            1,
            stdout="Fatal: embedding provider not configured\n",
            stderr="",
        )
        exc_info = self._run_semantic_worker_with_results(init_ok, sem_fail)
        error_msg = str(exc_info.value)
        assert "Fatal: embedding provider not configured" in error_msg
        assert error_msg.rstrip() != "Failed to create semantic index:"

    def test_semantic_exit_code_used_when_both_empty(self):
        """When cidx index (semantic) fails with both outputs empty, uses exit code."""
        init_ok = _make_subprocess_result(0, stdout="initialized", stderr="")
        sem_fail = _make_subprocess_result(99, stdout="", stderr="")
        exc_info = self._run_semantic_worker_with_results(init_ok, sem_fail)
        assert "99" in str(exc_info.value)


# ---------------------------------------------------------------------------
# cidx index --rebuild-fts-index (FTS) failure
# ---------------------------------------------------------------------------


class TestFTSIndexErrorMessages:
    """Tests for cidx index --rebuild-fts-index (FTS) failure error message fallback."""

    def _run_fts_worker_with_results(self, init_result, fts_result):
        manager, GoldenRepoError = _make_manager_for_add_index()
        worker = _extract_background_worker(manager, "fts")

        call_count = [0]

        def side_effect(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return init_result
            return fts_result

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(GoldenRepoError) as exc_info:
                worker()

        return exc_info

    def test_fts_stderr_used_when_present(self):
        """When FTS index fails with stderr content, error uses stderr."""
        init_ok = _make_subprocess_result(0, stdout="initialized", stderr="")
        fts_fail = _make_subprocess_result(1, stdout="", stderr="Tantivy schema error")
        exc_info = self._run_fts_worker_with_results(init_ok, fts_fail)
        assert "Tantivy schema error" in str(exc_info.value)

    def test_fts_stdout_used_when_stderr_empty(self):
        """When FTS index fails with empty stderr, error falls back to stdout."""
        init_ok = _make_subprocess_result(0, stdout="initialized", stderr="")
        fts_fail = _make_subprocess_result(
            1, stdout="Error: FTS directory already locked\n", stderr=""
        )
        exc_info = self._run_fts_worker_with_results(init_ok, fts_fail)
        error_msg = str(exc_info.value)
        assert "Error: FTS directory already locked" in error_msg
        assert error_msg.rstrip() != "Failed to create FTS index:"

    def test_fts_exit_code_used_when_both_empty(self):
        """When FTS index fails with both outputs empty, uses exit code."""
        init_ok = _make_subprocess_result(0, stdout="initialized", stderr="")
        fts_fail = _make_subprocess_result(7, stdout="", stderr="")
        exc_info = self._run_fts_worker_with_results(init_ok, fts_fail)
        assert "7" in str(exc_info.value)


# ---------------------------------------------------------------------------
# cidx index --index-commits (temporal) failure
# ---------------------------------------------------------------------------


class TestTemporalIndexErrorMessages:
    """Tests for cidx index --index-commits (temporal) failure error message fallback."""

    def _run_temporal_worker_with_results(self, init_result, temporal_result):
        manager, GoldenRepoError = _make_manager_for_add_index()
        worker = _extract_background_worker(manager, "temporal")

        call_count = [0]

        def side_effect(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return init_result
            return temporal_result

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(GoldenRepoError) as exc_info:
                worker()

        return exc_info

    def test_temporal_stderr_used_when_present(self):
        """When temporal index fails with stderr content, error uses stderr."""
        init_ok = _make_subprocess_result(0, stdout="initialized", stderr="")
        temp_fail = _make_subprocess_result(
            1, stdout="", stderr="git: not a git repository"
        )
        exc_info = self._run_temporal_worker_with_results(init_ok, temp_fail)
        assert "git: not a git repository" in str(exc_info.value)

    def test_temporal_stdout_used_when_stderr_empty(self):
        """When temporal index fails with empty stderr, error falls back to stdout."""
        init_ok = _make_subprocess_result(0, stdout="initialized", stderr="")
        temp_fail = _make_subprocess_result(
            1, stdout="Error: no commits found in repository\n", stderr=""
        )
        exc_info = self._run_temporal_worker_with_results(init_ok, temp_fail)
        error_msg = str(exc_info.value)
        assert "Error: no commits found in repository" in error_msg
        assert error_msg.rstrip() != "Failed to create temporal index:"

    def test_temporal_exit_code_used_when_both_empty(self):
        """When temporal index fails with both outputs empty, uses exit code."""
        init_ok = _make_subprocess_result(0, stdout="initialized", stderr="")
        temp_fail = _make_subprocess_result(3, stdout="", stderr="")
        exc_info = self._run_temporal_worker_with_results(init_ok, temp_fail)
        assert "3" in str(exc_info.value)


# ---------------------------------------------------------------------------
# cidx scip generate (SCIP) failure
# ---------------------------------------------------------------------------


class TestSCIPIndexErrorMessages:
    """Tests for cidx scip generate (SCIP) failure error message fallback chain."""

    def _run_scip_worker_with_results(self, init_result, scip_result):
        manager, GoldenRepoError = _make_manager_for_add_index()
        worker = _extract_background_worker(manager, "scip")

        call_count = [0]

        def side_effect(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return init_result
            return scip_result

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(GoldenRepoError) as exc_info:
                worker()

        return exc_info

    def test_scip_stderr_used_when_present(self):
        """When SCIP generation fails with stderr content, error uses stderr."""
        init_ok = _make_subprocess_result(0, stdout="initialized", stderr="")
        scip_fail = _make_subprocess_result(
            1, stdout="", stderr="scip-python: command not found"
        )
        exc_info = self._run_scip_worker_with_results(init_ok, scip_fail)
        assert "scip-python: command not found" in str(exc_info.value)

    def test_scip_stdout_used_when_stderr_empty(self):
        """When SCIP generation fails with empty stderr, error falls back to stdout."""
        init_ok = _make_subprocess_result(0, stdout="initialized", stderr="")
        scip_fail = _make_subprocess_result(
            1, stdout="Error: unsupported language for SCIP indexing\n", stderr=""
        )
        exc_info = self._run_scip_worker_with_results(init_ok, scip_fail)
        error_msg = str(exc_info.value)
        assert "Error: unsupported language for SCIP indexing" in error_msg
        assert error_msg.rstrip() != "Failed to create SCIP index:"

    def test_scip_exit_code_used_when_both_empty(self):
        """When SCIP generation fails with both outputs empty, uses exit code."""
        init_ok = _make_subprocess_result(0, stdout="initialized", stderr="")
        scip_fail = _make_subprocess_result(5, stdout="", stderr="")
        exc_info = self._run_scip_worker_with_results(init_ok, scip_fail)
        assert "5" in str(exc_info.value)
