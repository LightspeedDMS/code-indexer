"""
Bug #1218 — Remove ALL overarching job/subprocess/per-file timeouts on the
indexing+registration+SCIP path.

These tests assert:

A. progress_subprocess_runner.run_with_popen_progress has NO `timeout` parameter
   and NO watchdog/SIGKILL code.

B. file_chunking_manager: VECTOR_PROCESSING_TIMEOUT constant removed and
   future.result() called without a timeout argument.

C. high_throughput_processor: file_result_timeout variable removed and
   TimeoutError swallow handler removed.

D. temporal_indexer: future.result(timeout=30) on commit-message embedding removed.

E. config_manager: ScipConfig no longer has indexing_timeout_seconds,
   scip_generation_timeout_seconds, or registration_indexing_timeout_seconds.

F. config_service: no read/write wiring for the removed config fields.

G. golden_repo_manager: registration-path no longer passes timeout= to
   run_with_popen_progress; registration failure removes the clone directory.

H. activated_repo_index_manager: no longer passes timeout to subprocess.run
   for indexing; no TimeoutExpired handler on the indexing path.

I. refresh_scheduler: SCIP generation in _index_source has no timeout= and
   no TimeoutExpired handler.

J. Behavior: a genuine embedding failure propagates out of file_chunking_manager
   (not swallowed/skipped).

K. Behavior: registration failure cleans up its clone directory.
"""

from __future__ import annotations

import ast
import inspect
from typing import List
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helper: parse source of a function/method as AST
# ---------------------------------------------------------------------------


def _parse_source(func) -> ast.Module:
    src = inspect.getsource(func)
    # dedent handles indented class methods
    import textwrap

    return ast.parse(textwrap.dedent(src))


def _source_text(func) -> str:
    import textwrap

    return textwrap.dedent(inspect.getsource(func))


# ---------------------------------------------------------------------------
# A. progress_subprocess_runner — no timeout parameter, no watchdog/SIGKILL
# ---------------------------------------------------------------------------


class TestProgressSubprocessRunnerNoTimeout:
    """Bug #1218-A: run_with_popen_progress must have no timeout parameter."""

    def test_run_with_popen_progress_has_no_timeout_parameter(self):
        """The `timeout` parameter must be removed from run_with_popen_progress."""
        from code_indexer.services.progress_subprocess_runner import (
            run_with_popen_progress,
        )
        import inspect

        sig = inspect.signature(run_with_popen_progress)
        assert "timeout" not in sig.parameters, (
            "run_with_popen_progress still has a `timeout` parameter. "
            "Bug #1218 requires removing it entirely."
        )

    def test_no_os_killpg_in_progress_subprocess_runner(self):
        """os.killpg (SIGKILL watchdog) must be removed from progress_subprocess_runner."""
        import code_indexer.services.progress_subprocess_runner as mod

        src = inspect.getsource(mod)
        assert "os.killpg" not in src, (
            "progress_subprocess_runner still contains os.killpg. "
            "Bug #1218 requires removing the whole watchdog/SIGKILL block."
        )

    def test_no_watchdog_thread_in_progress_subprocess_runner(self):
        """The watchdog threading.Thread must be removed from run_with_popen_progress."""
        from code_indexer.services.progress_subprocess_runner import (
            run_with_popen_progress,
        )

        src = _source_text(run_with_popen_progress)
        # The watchdog pattern was: if timeout is not None: def _watchdog() ...
        assert "_watchdog" not in src, (
            "progress_subprocess_runner still contains _watchdog (watchdog thread). "
            "Bug #1218 requires removing it entirely."
        )

    def test_no_timed_out_sentinel_in_progress_subprocess_runner(self):
        """The timed_out Event sentinel used by the watchdog must be removed."""
        from code_indexer.services.progress_subprocess_runner import (
            run_with_popen_progress,
        )

        src = _source_text(run_with_popen_progress)
        assert "timed_out" not in src, (
            "progress_subprocess_runner still has `timed_out` Event (watchdog sentinel). "
            "Bug #1218 requires removing it."
        )

    def test_no_returncode_minus9_timeout_check(self):
        """The `returncode == -9` timeout detection block must be removed."""
        from code_indexer.services.progress_subprocess_runner import (
            run_with_popen_progress,
        )

        src = _source_text(run_with_popen_progress)
        assert "returncode == -9" not in src and "returncode==-9" not in src, (
            "progress_subprocess_runner still checks returncode==-9 for timeout. "
            "Bug #1218 requires removing this check."
        )

    def test_no_timed_out_after_message(self):
        """The 'Timed out after' error message must be removed."""
        import code_indexer.services.progress_subprocess_runner as mod

        src = inspect.getsource(mod)
        assert "Timed out after" not in src, (
            "progress_subprocess_runner still emits 'Timed out after' message. "
            "Bug #1218 requires removing this."
        )


# ---------------------------------------------------------------------------
# B. file_chunking_manager — no VECTOR_PROCESSING_TIMEOUT, no future.result(timeout=...)
# ---------------------------------------------------------------------------


class TestFileChunkingManagerNoTimeout:
    """Bug #1218-B: VECTOR_PROCESSING_TIMEOUT and future.result(timeout=) must be removed."""

    def test_vector_processing_timeout_constant_removed(self):
        """VECTOR_PROCESSING_TIMEOUT constant must be deleted from file_chunking_manager."""
        import code_indexer.services.file_chunking_manager as mod

        assert not hasattr(mod, "VECTOR_PROCESSING_TIMEOUT"), (
            "file_chunking_manager still exports VECTOR_PROCESSING_TIMEOUT. "
            "Bug #1218 requires removing this constant entirely."
        )

    def test_no_future_result_with_timeout_in_process_file(self):
        """future.result(timeout=...) must not exist in the file processing path."""
        from code_indexer.services.file_chunking_manager import FileChunkingManager

        # Check _process_single_file or process_file method
        src = inspect.getsource(FileChunkingManager)
        # We must not see VECTOR_PROCESSING_TIMEOUT nor future.result(timeout=
        assert "VECTOR_PROCESSING_TIMEOUT" not in src, (
            "FileChunkingManager still references VECTOR_PROCESSING_TIMEOUT."
        )
        # Check for timeout= inside result() calls
        # Parse AST to find future.result(timeout=...) pattern
        import textwrap

        tree = ast.parse(textwrap.dedent(src))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Look for .result(timeout=...) calls
                if isinstance(node.func, ast.Attribute) and node.func.attr == "result":
                    timeout_kws = [kw for kw in node.keywords if kw.arg == "timeout"]
                    assert not timeout_kws, (
                        f"FileChunkingManager still calls .result(timeout=...) at line "
                        f"{node.lineno}. Bug #1218 requires removing this timeout."
                    )


# ---------------------------------------------------------------------------
# C. high_throughput_processor — no file_result_timeout, no TimeoutError swallow
# ---------------------------------------------------------------------------


class TestHighThroughputProcessorNoTimeout:
    """Bug #1218-C: file_result_timeout and concurrent.futures.TimeoutError handler removed."""

    def test_no_file_result_timeout_variable(self):
        """file_result_timeout variable must be removed from high_throughput_processor."""
        import code_indexer.services.high_throughput_processor as mod

        src = inspect.getsource(mod)
        assert "file_result_timeout" not in src, (
            "high_throughput_processor still has file_result_timeout variable. "
            "Bug #1218 requires removing this per-file timeout."
        )

    def test_no_concurrent_futures_timeouterror_swallow(self):
        """concurrent.futures.TimeoutError handler that swallows and skips files must be removed."""
        import code_indexer.services.high_throughput_processor as mod

        src = inspect.getsource(mod)
        # The original code was: except concurrent.futures.TimeoutError:
        # followed by stats.failed_files += 1 / continue (skip logic)
        assert "except concurrent.futures.TimeoutError" not in src, (
            "high_throughput_processor still has 'except concurrent.futures.TimeoutError' "
            "swallow handler. Bug #1218 requires removing this."
        )

    def test_no_file_future_result_with_timeout(self):
        """file_future.result(timeout=...) must not exist on the non-cancel path."""
        import code_indexer.services.high_throughput_processor as mod

        src = inspect.getsource(mod)
        # We need to check that file_future.result() has no timeout argument
        # Use AST to be precise
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr == "result":
                    timeout_kws = [kw for kw in node.keywords if kw.arg == "timeout"]
                    assert not timeout_kws, (
                        f"high_throughput_processor still calls .result(timeout=...) "
                        f"at line {node.lineno}. Bug #1218 requires removing this."
                    )


# ---------------------------------------------------------------------------
# D. temporal_indexer — future.result(timeout=30) on commit embedding removed
# ---------------------------------------------------------------------------


class TestTemporalIndexerNoTimeout:
    """Bug #1218-D: future.result(timeout=30) on commit-message embedding must be removed."""

    def test_no_future_result_timeout_in_index_commit(self):
        """The future.result(timeout=30) call in commit-message indexing must be removed."""
        from code_indexer.services.temporal.temporal_indexer import TemporalIndexer

        # Find the _index_commit_message or similar method
        src = inspect.getsource(TemporalIndexer)
        import textwrap

        tree = ast.parse(textwrap.dedent(src))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr == "result":
                    timeout_kws = [kw for kw in node.keywords if kw.arg == "timeout"]
                    assert not timeout_kws, (
                        f"TemporalIndexer still calls future.result(timeout=...) "
                        f"at line {node.lineno}. Bug #1218 requires removing this."
                    )


# ---------------------------------------------------------------------------
# E. config_manager — removed ScipConfig fields
# ---------------------------------------------------------------------------


class TestScipConfigFieldsRemoved:
    """Bug #1218-E: indexing_timeout_seconds, scip_generation_timeout_seconds,
    and registration_indexing_timeout_seconds must be removed from ScipConfig."""

    def test_indexing_timeout_seconds_removed_from_scip_config(self):
        """ScipConfig must NOT have indexing_timeout_seconds field."""
        from code_indexer.server.utils.config_manager import ScipConfig

        config = ScipConfig()
        assert not hasattr(config, "indexing_timeout_seconds"), (
            "ScipConfig still has indexing_timeout_seconds. "
            "Bug #1218 requires removing this field."
        )

    def test_scip_generation_timeout_seconds_removed_from_scip_config(self):
        """ScipConfig must NOT have scip_generation_timeout_seconds field."""
        from code_indexer.server.utils.config_manager import ScipConfig

        config = ScipConfig()
        assert not hasattr(config, "scip_generation_timeout_seconds"), (
            "ScipConfig still has scip_generation_timeout_seconds. "
            "Bug #1218 requires removing this field."
        )

    def test_registration_indexing_timeout_seconds_removed_from_scip_config(self):
        """ScipConfig must NOT have registration_indexing_timeout_seconds field."""
        from code_indexer.server.utils.config_manager import ScipConfig

        config = ScipConfig()
        assert not hasattr(config, "registration_indexing_timeout_seconds"), (
            "ScipConfig still has registration_indexing_timeout_seconds. "
            "Bug #1218 requires removing this field."
        )

    def test_no_indexing_timeout_constant_in_activated_repo_index_manager(self):
        """INDEXING_TIMEOUT_SECONDS class constant must be removed from ActivatedRepoIndexManager."""
        from code_indexer.server.services.activated_repo_index_manager import (
            ActivatedRepoIndexManager,
        )

        assert not hasattr(ActivatedRepoIndexManager, "INDEXING_TIMEOUT_SECONDS"), (
            "ActivatedRepoIndexManager still has INDEXING_TIMEOUT_SECONDS class constant. "
            "Bug #1218 requires removing this."
        )

    def test_no_scip_timeout_constant_in_activated_repo_index_manager(self):
        """SCIP_TIMEOUT_SECONDS class constant must be removed from ActivatedRepoIndexManager."""
        from code_indexer.server.services.activated_repo_index_manager import (
            ActivatedRepoIndexManager,
        )

        assert not hasattr(ActivatedRepoIndexManager, "SCIP_TIMEOUT_SECONDS"), (
            "ActivatedRepoIndexManager still has SCIP_TIMEOUT_SECONDS class constant. "
            "Bug #1218 requires removing this."
        )


# ---------------------------------------------------------------------------
# F. config_service — no read/write wiring for removed fields
# ---------------------------------------------------------------------------


class TestConfigServiceRemovedFields:
    """Bug #1218-F: config_service must not read/write the removed timeout fields."""

    def test_no_indexing_timeout_in_config_service_get_config(self):
        """config_service._get_config_dict must not include indexing_timeout_seconds."""
        import code_indexer.server.services.config_service as mod

        src = inspect.getsource(mod)
        # The _update_scip_setting should not handle indexing_timeout_seconds
        assert '"indexing_timeout_seconds"' not in src, (
            "config_service still references indexing_timeout_seconds. "
            "Bug #1218 requires removing this wiring."
        )

    def test_no_scip_generation_timeout_in_config_service(self):
        """config_service must not reference scip_generation_timeout_seconds."""
        import code_indexer.server.services.config_service as mod

        src = inspect.getsource(mod)
        assert '"scip_generation_timeout_seconds"' not in src, (
            "config_service still references scip_generation_timeout_seconds. "
            "Bug #1218 requires removing this wiring."
        )


# ---------------------------------------------------------------------------
# G. golden_repo_manager — no timeout= to run_with_popen_progress on registration path
# ---------------------------------------------------------------------------


class TestGoldenRepoManagerNoIndexingTimeout:
    """Bug #1218-G: registration path must not pass timeout= to run_with_popen_progress."""

    def test_execute_post_clone_workflow_passes_no_timeout(self):
        """_execute_post_clone_workflow must NOT pass timeout= to run_with_popen_progress."""
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoManager,
        )

        src = _source_text(GoldenRepoManager._execute_post_clone_workflow)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Look for run_with_popen_progress calls with timeout= kwarg
                func_name = ""
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                if "popen_progress" in func_name or "run_with_popen" in func_name:
                    timeout_kws = [kw for kw in node.keywords if kw.arg == "timeout"]
                    assert not timeout_kws, (
                        f"_execute_post_clone_workflow still passes timeout= to "
                        f"run_with_popen_progress at line {node.lineno}. "
                        "Bug #1218 requires removing this."
                    )

    def test_no_registration_indexing_timeout_seconds_in_golden_repo_manager(self):
        """golden_repo_manager must not reference registration_indexing_timeout_seconds."""
        import code_indexer.server.repositories.golden_repo_manager as mod

        src = inspect.getsource(mod)
        assert "registration_indexing_timeout_seconds" not in src, (
            "golden_repo_manager still references registration_indexing_timeout_seconds. "
            "Bug #1218 requires removing this."
        )

    def test_no_indexing_timeout_variable_in_post_clone_workflow(self):
        """_execute_post_clone_workflow must not have _indexing_timeout local variable."""
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoManager,
        )

        src = _source_text(GoldenRepoManager._execute_post_clone_workflow)
        assert "_indexing_timeout" not in src, (
            "_execute_post_clone_workflow still has _indexing_timeout local variable. "
            "Bug #1218 requires removing this."
        )


# ---------------------------------------------------------------------------
# H. activated_repo_index_manager — no timeout on indexing subprocess, no TimeoutExpired
# ---------------------------------------------------------------------------


class TestActivatedRepoIndexManagerNoTimeout:
    """Bug #1218-H: activated_repo_index_manager must not use timeout on indexing subprocess."""

    def test_run_subprocess_with_telemetry_has_no_timeout_parameter(self):
        """_run_subprocess_with_telemetry must not have a timeout parameter."""
        from code_indexer.server.services.activated_repo_index_manager import (
            ActivatedRepoIndexManager,
        )

        sig = inspect.signature(
            ActivatedRepoIndexManager._run_subprocess_with_telemetry
        )
        assert "timeout" not in sig.parameters, (
            "_run_subprocess_with_telemetry still has a timeout parameter. "
            "Bug #1218 requires removing it."
        )

    def test_no_subprocess_timeoutexpired_in_execute_semantic(self):
        """_execute_semantic_indexing must not catch subprocess.TimeoutExpired."""
        from code_indexer.server.services.activated_repo_index_manager import (
            ActivatedRepoIndexManager,
        )

        src = _source_text(ActivatedRepoIndexManager._execute_semantic_indexing)
        assert "TimeoutExpired" not in src, (
            "_execute_semantic_indexing still has subprocess.TimeoutExpired handler. "
            "Bug #1218 requires removing it."
        )

    def test_no_subprocess_timeoutexpired_in_execute_fts(self):
        """_execute_fts_indexing must not catch subprocess.TimeoutExpired."""
        from code_indexer.server.services.activated_repo_index_manager import (
            ActivatedRepoIndexManager,
        )

        src = _source_text(ActivatedRepoIndexManager._execute_fts_indexing)
        assert "TimeoutExpired" not in src, (
            "_execute_fts_indexing still has subprocess.TimeoutExpired handler. "
            "Bug #1218 requires removing it."
        )

    def test_no_subprocess_timeoutexpired_in_execute_temporal(self):
        """_execute_temporal_indexing must not catch subprocess.TimeoutExpired."""
        from code_indexer.server.services.activated_repo_index_manager import (
            ActivatedRepoIndexManager,
        )

        src = _source_text(ActivatedRepoIndexManager._execute_temporal_indexing)
        assert "TimeoutExpired" not in src, (
            "_execute_temporal_indexing still has subprocess.TimeoutExpired handler. "
            "Bug #1218 requires removing it."
        )

    def test_no_subprocess_timeoutexpired_in_execute_scip(self):
        """_execute_scip_indexing must not catch subprocess.TimeoutExpired."""
        from code_indexer.server.services.activated_repo_index_manager import (
            ActivatedRepoIndexManager,
        )

        src = _source_text(ActivatedRepoIndexManager._execute_scip_indexing)
        assert "TimeoutExpired" not in src, (
            "_execute_scip_indexing still has subprocess.TimeoutExpired handler. "
            "Bug #1218 requires removing it."
        )

    def test_no_timeout_kwarg_in_run_subprocess_with_telemetry_body(self):
        """subprocess.run inside _run_subprocess_with_telemetry must not pass timeout=."""
        from code_indexer.server.services.activated_repo_index_manager import (
            ActivatedRepoIndexManager,
        )

        src = _source_text(ActivatedRepoIndexManager._run_subprocess_with_telemetry)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr == "run":
                    timeout_kws = [kw for kw in node.keywords if kw.arg == "timeout"]
                    assert not timeout_kws, (
                        f"_run_subprocess_with_telemetry passes timeout= to subprocess.run "
                        f"at line {node.lineno}. Bug #1218 requires removing this."
                    )


# ---------------------------------------------------------------------------
# I. refresh_scheduler — SCIP generation in _index_source has no timeout
# ---------------------------------------------------------------------------


class TestRefreshSchedulerNoScipTimeout:
    """Bug #1218-I: _index_source must NOT pass timeout= to SCIP subprocess.run."""

    def test_scip_generate_subprocess_run_has_no_timeout(self):
        """subprocess.run for SCIP generation in _index_source must have no timeout= kwarg."""
        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

        src = _source_text(RefreshScheduler._index_source)
        tree = ast.parse(src)
        # Find subprocess.run calls that mention scip
        # Strategy: find all subprocess.run calls in the method and assert none have timeout=
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "run":
                    timeout_kws = [kw for kw in node.keywords if kw.arg == "timeout"]
                    assert not timeout_kws, (
                        f"_index_source still passes timeout= to subprocess.run "
                        f"at line {node.lineno}. Bug #1218 requires removing SCIP timeout."
                    )

    def test_no_timeoutexpired_handler_in_index_source(self):
        """_index_source must not catch subprocess.TimeoutExpired."""
        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

        src = _source_text(RefreshScheduler._index_source)
        assert "TimeoutExpired" not in src, (
            "_index_source still has subprocess.TimeoutExpired handler. "
            "Bug #1218 requires removing this."
        )

    def test_no_scip_timeout_variable_in_index_source(self):
        """scip_timeout local variable must be removed from _index_source."""
        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

        src = _source_text(RefreshScheduler._index_source)
        assert "scip_timeout" not in src, (
            "_index_source still has scip_timeout variable. "
            "Bug #1218 requires removing it."
        )


# ---------------------------------------------------------------------------
# J. Behavior: genuine embedding failure propagates (anti-silent-failure)
# ---------------------------------------------------------------------------


class TestEmbeddingFailurePropagates:
    """Bug #1218-J: a post-retry embedding failure must PROPAGATE, not be swallowed."""

    def test_chunking_manager_propagates_embedding_failure(self, tmp_path):
        """When batch_future.result() raises RuntimeError (post-retry embed failure),
        _process_file_clean_lifecycle must reflect failure (not swallow silently).

        The design returns FileProcessingResult(success=False) for batch failures —
        that is acceptable because the caller (high_throughput_processor) counts it as
        failed_files and the job fails loudly.  What is NOT acceptable is swallowing a
        concurrent.futures.TimeoutError and counting it as failed_files without propagating.
        We test that a genuine RuntimeError from the future IS reflected as success=False.
        """
        from code_indexer.services.file_chunking_manager import (
            FileChunkingManager,
        )
        from code_indexer.services.clean_slot_tracker import CleanSlotTracker

        # Build a minimal FileChunkingManager
        mock_vector_mgr = MagicMock()

        # Simulate a future that raises RuntimeError (post-retry embedding failure)
        failing_future = MagicMock()
        failing_future.result.side_effect = RuntimeError(
            "Embedding provider failed after 3 retries: 503 Service Unavailable"
        )
        mock_vector_mgr.submit_batch_task.return_value = failing_future

        mock_slot_tracker = MagicMock(spec=CleanSlotTracker)
        mock_slot_tracker.acquire_slot.return_value = 1
        mock_slot_tracker.update_slot.return_value = None
        mock_slot_tracker.release_slot.return_value = None

        manager = FileChunkingManager.__new__(FileChunkingManager)
        manager.vector_manager = mock_vector_mgr
        manager.slot_tracker = mock_slot_tracker
        manager.thread_count = 1
        manager.chunk_size = 512
        manager.overlap = 0

        # Create a real file to process
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo():\n    pass\n" * 10)

        metadata = {
            "repo": "test",
            "branch": "main",
            "file_path": str(test_file),
            "git_hash": "abc123",
        }

        # Call _process_file_clean_lifecycle — should return failure, not swallow
        result = manager._process_file_clean_lifecycle(
            test_file, metadata, progress_callback=None, slot_tracker=mock_slot_tracker
        )

        # Must reflect failure (not success=True)
        assert result.success is False, (
            "FileChunkingManager returned success=True after embedding failure. "
            "Bug #1218 requires failures to be visible."
        )
        assert result.error is not None, (
            "FileChunkingManager returned no error message after embedding failure."
        )


# ---------------------------------------------------------------------------
# K. Behavior: registration failure must remove the clone directory
# ---------------------------------------------------------------------------


class TestRegistrationFailureCleanup:
    """Bug #1218-K: when golden repo registration fails after cloning,
    the clone directory must be removed so retries work."""

    def test_registration_failure_removes_clone_dir(self, tmp_path):
        """If _execute_post_clone_workflow raises, the background_worker must
        shutil.rmtree the clone_path so the directory is gone after failure."""
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoManager,
            GitOperationError,
        )

        # Create a fake clone directory that will "exist" after cloning
        clone_dir = tmp_path / "fake-clone"
        clone_dir.mkdir()

        manager = GoldenRepoManager(data_dir=str(tmp_path))

        # background_job_manager is runtime-injected (not in __init__), inject it now
        mock_bgm = MagicMock()
        submitted_func: List = []

        def capture_submit(**kwargs):
            submitted_func.append(kwargs.get("func"))
            return "fake-job-id"

        mock_bgm.submit_job.side_effect = capture_submit
        manager.background_job_manager = mock_bgm  # type: ignore[attr-defined]

        def fake_clone(repo_url, alias, branch):
            # Return the clone_dir path (simulating successful clone)
            return str(clone_dir)

        def fake_post_clone(*args, **kwargs):
            # Simulate failure during post-clone (e.g. indexing timeout killed it before fix)
            raise GitOperationError("indexing failed")

        with (
            patch.object(manager, "_clone_repository", side_effect=fake_clone),
            patch.object(
                manager, "_execute_post_clone_workflow", side_effect=fake_post_clone
            ),
        ):
            manager.add_golden_repo(
                repo_url="https://github.com/test/repo",
                alias="test-repo",
                submitter_username="admin",
                skip_pre_flight_git_validation=True,
            )

            assert submitted_func, "No function was submitted to BackgroundJobManager"
            worker_fn = submitted_func[0]

            # Clone dir exists before the worker runs
            assert clone_dir.exists(), "Pre-condition: clone_dir should exist"

            # Run the background worker inside the patch context so the fake
            # _clone_repository and _execute_post_clone_workflow are still active.
            try:
                worker_fn()
            except (GitOperationError, Exception):
                pass  # Failure is expected — but cleanup must have happened

        # Post-condition: clone_dir must have been removed
        assert not clone_dir.exists(), (
            f"clone_dir still exists after registration failure: {clone_dir}. "
            "Bug #1218 requires cleanup on failure so retries don't hit "
            "'destination path already exists'."
        )


def _make_index_cli_mocks(stats_override):
    """Build the patch context managers needed to drive cli `index` to the
    completion block with a fully-controlled ProcessingStats result.

    Patches (all at module level where they are imported):
      - ConfigManager.create_with_backtrack  -> mock that yields a config with
        daemon disabled and a single voyage-ai provider
      - EmbeddingProviderFactory.create      -> healthy mock embedding provider
      - EmbeddingProviderFactory.resolve_api_key -> always returns a fake key
      - BackendFactory.create                -> healthy mock backend
      - SmartIndexer (at source module)      -> returns stats_override from smart_index()
    """
    from unittest.mock import MagicMock

    from code_indexer.indexing.processor import ProcessingStats  # noqa: F401

    mock_config = MagicMock()
    mock_config.daemon = None  # disables daemon-delegation branch
    mock_config.embedding_provider = "voyage-ai"
    mock_config.codebase_dir = "/fake/codebase"
    mock_config.get_embedding_providers.return_value = ["voyage-ai"]
    mock_config.vector_store = None

    mock_config_manager = MagicMock()
    mock_config_manager.load.return_value = mock_config
    # config_path is used for metadata_path construction; keep as MagicMock
    # so Path / operations return further MagicMocks (not used downstream).

    mock_embedding = MagicMock()
    mock_embedding.health_check.return_value = True
    mock_embedding.get_provider_name.return_value = "voyage-ai"

    mock_backend = MagicMock()
    mock_backend.health_check.return_value = True
    mock_backend.get_vector_store_client.return_value = MagicMock()

    mock_smart_indexer = MagicMock()
    mock_smart_indexer.smart_index.return_value = stats_override
    mock_smart_indexer.get_indexing_status.return_value = {
        "status": "completed",
        "can_resume": False,
        "files_processed": 0,
        "chunks_indexed": 0,
    }
    mock_smart_indexer.get_git_status.return_value = {
        "git_available": False,
        "project_id": "fake-project",
    }
    mock_smart_indexer.slot_tracker = None

    return (
        patch(
            "code_indexer.cli.ConfigManager.create_with_backtrack",
            return_value=mock_config_manager,
        ),
        patch(
            "code_indexer.cli.EmbeddingProviderFactory.create",
            return_value=mock_embedding,
        ),
        patch(
            "code_indexer.cli.EmbeddingProviderFactory.resolve_api_key",
            return_value="fake-key",
        ),
        patch(
            "code_indexer.cli.BackendFactory.create",
            return_value=mock_backend,
        ),
        patch(
            "code_indexer.services.smart_indexer.SmartIndexer",
            return_value=mock_smart_indexer,
        ),
    )


class TestCliTotalFailureExitsNonzero:
    """Bug #1218 principle #4: No silent partial index.
    When files_processed==0 AND failed_files>0, the CLI must exit(1)
    so callers/scripts can detect that the index is empty due to failures.
    """

    def test_cli_exits_1_when_all_files_fail(self):
        """Functional: CliRunner invokes `index`; when every file fails
        (files_processed=0, failed_files=3) the exit code must be 1."""
        from click.testing import CliRunner

        from code_indexer.cli import cli
        from code_indexer.indexing.processor import ProcessingStats

        stats = ProcessingStats(files_processed=0, failed_files=3, cancelled=False)
        patches = _make_index_cli_mocks(stats)
        runner = CliRunner()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = runner.invoke(cli, ["index"])

        assert result.exit_code == 1, (
            f"cidx index exited {result.exit_code} (expected 1) "
            "when files_processed=0 and failed_files=3. "
            "Bug #1218 principle #4: a completely-empty index must FAIL LOUD."
        )

    def test_cli_exits_0_on_partial_success(self):
        """Functional: CliRunner invokes `index`; when some files succeed
        (files_processed=5, failed_files=2) the exit code must be 0 —
        the guard must NOT over-fire on partial success."""
        from click.testing import CliRunner

        from code_indexer.cli import cli
        from code_indexer.indexing.processor import ProcessingStats

        stats = ProcessingStats(files_processed=5, failed_files=2, cancelled=False)
        patches = _make_index_cli_mocks(stats)
        runner = CliRunner()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = runner.invoke(cli, ["index"])

        assert result.exit_code == 0, (
            f"cidx index exited {result.exit_code} (expected 0) "
            "when files_processed=5 and failed_files=2. "
            "The total-failure guard must NOT fire on partial success."
        )

    def test_guard_present_after_failed_files_warning(self):
        """Source-inspection defense-in-depth: the sys.exit(1) guard must
        appear within 8 lines after the 'Failed files:' warning print."""
        from pathlib import Path

        cli_path = (
            Path(__file__).parent.parent.parent / "src" / "code_indexer" / "cli.py"
        )
        lines = cli_path.read_text().splitlines()

        warning_anchor = "Failed files:"
        warning_indices = [i for i, ln in enumerate(lines) if warning_anchor in ln]
        assert warning_indices, (
            f"Could not find '{warning_anchor}' print in cli.py — "
            "cannot verify total-failure guard placement."
        )

        guard_condition = "stats.files_processed == 0 and stats.failed_files > 0"
        exit_call = "sys.exit(1)"

        found = False
        for warn_idx in warning_indices:
            window = lines[warn_idx : warn_idx + 9]
            if any(guard_condition in ln for ln in window) and any(
                exit_call in ln for ln in window
            ):
                found = True
                break

        assert found, (
            f"cli.py does not contain both '{guard_condition}' AND "
            f"'{exit_call}' within 8 lines after the '{warning_anchor}' "
            "print. Bug #1218 principle #4: a completely-empty index "
            "must FAIL LOUD with sys.exit(1)."
        )
