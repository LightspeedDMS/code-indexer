"""
Regression tests for Phase-3 e2e flake:

  - Bug A: _execute_post_clone_workflow passes NO timeout to run_with_popen_progress.
    Under concurrent load, cidx index --fts stalls indefinitely; the 300 s e2e
    poll deadline fires before the job completes.

  - Bug B: ActivatedRepoIndexManager._execute_fts_indexing (and _execute_semantic)
    runs "cidx index" in a repo that has no .code-indexer/config.json.  cidx
    responds with "Command 'index' is not available in no configuration found".
    This occurs when the activated-repo's CoW-cloned .code-indexer dir is absent
    or the init step was skipped.

Fix A: _run_popen (inner closure) must forward a timeout from
       ScipConfig.indexing_timeout_seconds via run_with_popen_progress(..., timeout=...).

Fix B: _execute_fts_indexing and _execute_semantic_indexing must check that
       {repo_path}/.code-indexer/config.json exists before running cidx index.
       If absent, either run cidx init first or return a fast-fail error with
       a clear message so the job fails immediately instead of producing the
       confusing "no configuration found" error.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, call, patch

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)
from code_indexer.server.repositories.background_jobs import BackgroundJobManager


# ---------------------------------------------------------------------------
# Helper fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Return a minimal fake golden-repo directory with .code-indexer/config.json."""
    repo_dir = tmp_path / "fake-golden-repo"
    repo_dir.mkdir()
    cidx_dir = repo_dir / ".code-indexer"
    cidx_dir.mkdir()
    (cidx_dir / "config.json").write_text('{"codebase_dir": "/some/path"}')
    return repo_dir


@pytest.fixture
def tmp_repo_no_config(tmp_path: Path) -> Path:
    """Return a minimal fake activated-repo directory WITHOUT .code-indexer/config.json."""
    repo_dir = tmp_path / "fake-activated-repo"
    repo_dir.mkdir()
    # No .code-indexer directory at all — simulates repo cloned before init ran
    return repo_dir


@pytest.fixture
def golden_manager(tmp_path: Path) -> GoldenRepoManager:
    return GoldenRepoManager(data_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# Bug A — timeout must be forwarded in _execute_post_clone_workflow
# ---------------------------------------------------------------------------


class TestPostCloneWorkflowTimeout:
    """Bug A: _run_popen must pass timeout= to run_with_popen_progress."""

    def test_index_call_receives_timeout_argument(
        self, golden_manager: GoldenRepoManager, tmp_repo: Path
    ) -> None:
        """run_with_popen_progress called for cidx index must receive a non-None timeout.

        Before the fix, _run_popen forwarded no timeout (default=None), so a
        stalled cidx index subprocess would block indefinitely.
        """
        captured_kwargs: List[dict] = []

        def _mock_run_with_popen_progress(**kwargs) -> int:
            captured_kwargs.append(kwargs)
            # Simulate successful run
            return 0

        mock_subprocess_result = MagicMock()
        mock_subprocess_result.returncode = 0
        mock_subprocess_result.stdout = ""
        mock_subprocess_result.stderr = ""

        # run_with_popen_progress is lazily imported INSIDE _execute_post_clone_workflow,
        # so we must patch it at the source module, not the golden_repo_manager module.
        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=lambda *args, **kw: (captured_kwargs.append(kw) or 0),
            ),
            patch("subprocess.run", return_value=mock_subprocess_result),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
        ):
            golden_manager._execute_post_clone_workflow(
                clone_path=str(tmp_repo),
                force_init=False,
                enable_temporal=False,
                temporal_options=None,
            )

        # At least one popen call must have been made (the cidx index --fts call)
        assert len(captured_kwargs) >= 1, (
            "_execute_post_clone_workflow made no run_with_popen_progress calls"
        )

        for call_kw in captured_kwargs:
            timeout_val = call_kw.get("timeout")
            assert timeout_val is not None, (
                f"run_with_popen_progress was called without timeout= (got None). "
                f"Full kwargs: {call_kw}. "
                "Fix: pass indexing_timeout_seconds from ScipConfig to _run_popen."
            )
            assert isinstance(timeout_val, (int, float)), (
                f"timeout must be numeric, got {type(timeout_val)}"
            )
            assert timeout_val > 0, f"timeout must be > 0, got {timeout_val}"

    def test_timeout_value_is_at_least_60_seconds(
        self, golden_manager: GoldenRepoManager, tmp_repo: Path
    ) -> None:
        """Timeout must be a meaningful lower bound (>= 60 s) to handle large repos."""
        captured_kwargs: List[dict] = []

        mock_subprocess_result = MagicMock()
        mock_subprocess_result.returncode = 0
        mock_subprocess_result.stdout = ""
        mock_subprocess_result.stderr = ""

        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=lambda *args, **kw: (captured_kwargs.append(kw) or 0),
            ),
            patch("subprocess.run", return_value=mock_subprocess_result),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
        ):
            golden_manager._execute_post_clone_workflow(
                clone_path=str(tmp_repo),
                force_init=False,
                enable_temporal=False,
                temporal_options=None,
            )

        for call_kw in captured_kwargs:
            timeout_val = call_kw.get("timeout", 0)
            assert timeout_val >= 60, (
                f"timeout {timeout_val}s is too small for production indexing. "
                "Expected >= 60 s (ScipConfig.indexing_timeout_seconds default is 3600)."
            )


# ---------------------------------------------------------------------------
# Bug B — ActivatedRepoIndexManager init guard
# ---------------------------------------------------------------------------


class TestActivatedRepoIndexManagerInitGuard:
    """Bug B: _execute_fts_indexing must guard against missing config.json."""

    @pytest.fixture
    def mock_job_manager(self) -> MagicMock:
        mgr = MagicMock(spec=BackgroundJobManager)
        mgr.submit_job.return_value = "fake-job-id"
        mgr.list_jobs.return_value = {"jobs": []}
        return mgr

    @pytest.fixture
    def mock_activated_manager(self, tmp_path: Path) -> MagicMock:
        mgr = MagicMock()
        mgr.get_activated_repo_path.return_value = str(
            tmp_path / "activated-repos" / "admin" / "testrepo"
        )
        return mgr

    @pytest.fixture
    def index_manager(
        self,
        tmp_path: Path,
        mock_job_manager: MagicMock,
        mock_activated_manager: MagicMock,
    ) -> ActivatedRepoIndexManager:
        return ActivatedRepoIndexManager(
            data_dir=str(tmp_path),
            background_job_manager=mock_job_manager,
            activated_repo_manager=mock_activated_manager,
        )

    def test_fts_indexing_on_uninitialized_repo_fails_fast_with_clear_error(
        self,
        index_manager: ActivatedRepoIndexManager,
        tmp_repo_no_config: Path,
    ) -> None:
        """_execute_fts_indexing on a repo with no .code-indexer/config.json
        must NOT silently run cidx index (which would produce the cryptic
        'no configuration found' error).  It must either init the repo first
        or return a fast-fail dict with success=False and a message that
        clearly explains the repo is not initialized.
        """
        result = index_manager._execute_fts_indexing(
            str(tmp_repo_no_config), clear=False
        )

        # The result must indicate failure
        assert result.get("success") is False, (
            f"_execute_fts_indexing on uninitialized repo returned success=True. "
            f"Result: {result}"
        )

        # The error message must be clear about the initialization problem
        error_msg = (result.get("error") or "").lower()
        assert any(
            keyword in error_msg
            for keyword in ("init", "config", "not initialized", "configuration")
        ), (
            f"_execute_fts_indexing error message does not mention init/config problem: "
            f"{error_msg!r}. "
            "The error should tell the operator the repo needs cidx init."
        )

    def test_semantic_indexing_on_uninitialized_repo_fails_fast_with_clear_error(
        self,
        index_manager: ActivatedRepoIndexManager,
        tmp_repo_no_config: Path,
    ) -> None:
        """Same guard must apply to _execute_semantic_indexing."""
        result = index_manager._execute_semantic_indexing(
            str(tmp_repo_no_config), clear=False
        )

        assert result.get("success") is False, (
            f"_execute_semantic_indexing on uninitialized repo returned success=True. "
            f"Result: {result}"
        )

        error_msg = (result.get("error") or "").lower()
        assert any(
            keyword in error_msg
            for keyword in ("init", "config", "not initialized", "configuration")
        ), (
            f"_execute_semantic_indexing error message does not mention init/config: "
            f"{error_msg!r}."
        )

    def test_fts_indexing_on_initialized_repo_proceeds_normally(
        self,
        index_manager: ActivatedRepoIndexManager,
        tmp_repo: Path,
    ) -> None:
        """When .code-indexer/config.json EXISTS, _execute_fts_indexing must
        proceed to run cidx index (not short-circuit).
        """
        captured_calls: List[List[str]] = []

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = ""
        fake_result.stderr = ""

        original_run = index_manager._run_subprocess_with_telemetry

        def _capture_run(
            args: List[str], repo_path: str, timeout: int
        ) -> MagicMock:
            captured_calls.append(args)
            return fake_result

        index_manager._run_subprocess_with_telemetry = _capture_run  # type: ignore[method-assign]

        result = index_manager._execute_fts_indexing(str(tmp_repo), clear=False)

        # Must have actually called cidx index
        assert len(captured_calls) >= 1, (
            "_execute_fts_indexing did not call _run_subprocess_with_telemetry "
            "even though config.json exists."
        )
        cidx_call = captured_calls[0]
        assert "cidx" in cidx_call[0] or "index" in cidx_call, (
            f"Expected a cidx index call, got: {cidx_call}"
        )

    def test_semantic_indexing_on_initialized_repo_proceeds_normally(
        self,
        index_manager: ActivatedRepoIndexManager,
        tmp_repo: Path,
    ) -> None:
        """When .code-indexer/config.json EXISTS, _execute_semantic_indexing must proceed."""
        captured_calls: List[List[str]] = []

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = ""
        fake_result.stderr = ""

        def _capture_run(
            args: List[str], repo_path: str, timeout: int
        ) -> MagicMock:
            captured_calls.append(args)
            return fake_result

        index_manager._run_subprocess_with_telemetry = _capture_run  # type: ignore[method-assign]

        result = index_manager._execute_semantic_indexing(str(tmp_repo), clear=False)

        assert len(captured_calls) >= 1, (
            "_execute_semantic_indexing did not call _run_subprocess_with_telemetry "
            "even though config.json exists."
        )
