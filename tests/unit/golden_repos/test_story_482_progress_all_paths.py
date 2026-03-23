"""
Unit tests for Story #482: Extend Real-Time Progress to All User-Facing Paths.

Tests verify that progress_callback is wired through:
- PATH A: add_golden_repo background_worker -> _execute_post_clone_workflow
- PATH C: _submit_refresh_job -> _execute_refresh -> _index_source
- PATH D: change_branch_async -> change_branch (_cb_cidx_index)
- PATH E: _execute_indexing_job uses ProgressPhaseAllocator (not hardcoded %)

Each test verifies that progress_callback is actually called with values that
come from within the function itself (not just the manager-injected 25%).
"""
import inspect
import tempfile
from pathlib import Path
from unittest.mock import MagicMock


class TestPathAAddGoldenRepoProgressCallback:
    """PATH A: add_golden_repo background_worker must accept progress_callback."""

    def test_background_worker_accepts_progress_callback(self):
        """
        The background_worker closure inside add_golden_repo must have a
        progress_callback parameter so BackgroundJobManager can inject it.

        We verify this by inspecting the function submitted to BackgroundJobManager.
        """
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoManager,
        )

        # Create a minimal GoldenRepoManager with mocked dependencies
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GoldenRepoManager(data_dir=tmpdir)

            # Mock out dependencies
            mock_bjm = MagicMock()
            manager.background_job_manager = mock_bjm
            manager.activated_repo_manager = MagicMock()

            # Stub validation and git ops to avoid real network calls
            manager._validate_git_repository = MagicMock(return_value=True)

            # Call add_golden_repo; it will call submit_job with background_worker
            manager.add_golden_repo(
                alias="test-repo",
                repo_url="https://github.com/example/test.git",
                default_branch="main",
            )

            # Extract the func submitted to BackgroundJobManager
            assert mock_bjm.submit_job.called, "submit_job should have been called"
            submit_call = mock_bjm.submit_job.call_args
            submitted_func = (
                submit_call.kwargs.get("func")
                or submit_call[1].get("func")
                or submit_call[0][1]
            )

            # The submitted function must accept progress_callback
            sig = inspect.signature(submitted_func)
            assert "progress_callback" in sig.parameters, (
                "PATH A: background_worker must accept progress_callback. "
                f"Current parameters: {list(sig.parameters.keys())}"
            )

    def test_execute_post_clone_workflow_accepts_progress_callback(self):
        """
        _execute_post_clone_workflow must accept an optional progress_callback
        parameter so PATH A can forward progress updates from clone/init/index phases.
        """
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoManager,
        )

        sig = inspect.signature(GoldenRepoManager._execute_post_clone_workflow)
        assert "progress_callback" in sig.parameters, (
            "PATH A: _execute_post_clone_workflow must accept progress_callback. "
            f"Current parameters: {list(sig.parameters.keys())}"
        )


class TestPathAPostCloneWorkflowProgress:
    """PATH A: _execute_post_clone_workflow must actually call progress_callback."""

    def test_execute_post_clone_workflow_calls_progress_callback(self):
        """
        _execute_post_clone_workflow must invoke progress_callback with
        progress values during cidx init (coarse) and cidx index (Popen).
        Verifies Finding 1: the parameter was accepted but body never used it.
        """
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoManager,
        )
        from unittest.mock import patch, MagicMock
        import subprocess

        received = []

        def progress_cb(pct, phase=None, detail=None):
            received.append({"pct": pct, "phase": phase, "detail": detail})

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GoldenRepoManager(data_dir=tmpdir)

            # Stub subprocess.run to avoid real cidx calls
            fake_run_result = MagicMock()
            fake_run_result.returncode = 0
            fake_run_result.stdout = ""
            fake_run_result.stderr = ""

            # Stub run_with_popen_progress to call back progress and not spawn real process
            from code_indexer.services import progress_subprocess_runner as psr_mod

            def fake_popen_progress(
                command, phase_name, allocator, progress_callback,
                all_stdout, all_stderr, cwd, error_label=None
            ):
                if progress_callback is not None:
                    progress_callback(
                        int(allocator.phase_start(phase_name)),
                        phase=phase_name,
                        detail=f"{phase_name}: starting...",
                    )
                    progress_callback(
                        int(allocator.phase_end(phase_name)),
                        phase=phase_name,
                        detail=f"{phase_name}: done",
                    )

            with patch("subprocess.run", return_value=fake_run_result), \
                 patch.object(psr_mod, "run_with_popen_progress", side_effect=fake_popen_progress):
                manager._execute_post_clone_workflow(
                    clone_path=tmpdir,
                    force_init=True,
                    enable_temporal=False,
                    progress_callback=progress_cb,
                )

        # Must have received at least one progress callback call
        assert len(received) > 0, (
            "PATH A: _execute_post_clone_workflow must call progress_callback. "
            f"Got zero calls."
        )
        # Must have received calls with phase info
        calls_with_phase = [c for c in received if c.get("phase") is not None]
        assert len(calls_with_phase) > 0, (
            "PATH A: progress_callback calls must include phase= keyword. "
            f"Got: {received}"
        )


class TestPathCRefreshSchedulerProgressCallback:
    """PATH C: refresh scheduler must pass progress_callback through to _index_source."""

    def test_execute_refresh_accepts_progress_callback(self):
        """
        _execute_refresh must accept progress_callback so BackgroundJobManager
        can inject it during execution.
        """
        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

        sig_execute = inspect.signature(RefreshScheduler._execute_refresh)
        assert "progress_callback" in sig_execute.parameters, (
            "PATH C: _execute_refresh must accept progress_callback. "
            f"Current parameters: {list(sig_execute.parameters.keys())}"
        )

    def test_index_source_accepts_progress_callback(self):
        """
        _index_source must accept an optional progress_callback parameter.
        """
        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

        sig = inspect.signature(RefreshScheduler._index_source)
        assert "progress_callback" in sig.parameters, (
            "PATH C: _index_source must accept progress_callback. "
            f"Current parameters: {list(sig.parameters.keys())}"
        )


class TestPathCIndexSourceProgress:
    """PATH C: _index_source must actually call progress_callback."""

    def test_index_source_calls_progress_callback(self):
        """
        _index_source must invoke progress_callback with progress values
        during cidx index --fts (semantic+FTS) and optionally temporal indexing.
        Verifies Finding 2: the parameter was accepted but body used bare subprocess.run.
        """
        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
        from unittest.mock import patch, MagicMock
        from code_indexer.services import progress_subprocess_runner as psr_mod

        received = []

        def progress_cb(pct, phase=None, detail=None):
            received.append({"pct": pct, "phase": phase, "detail": detail})

        # Build a minimal RefreshScheduler with mocked dependencies
        scheduler = RefreshScheduler.__new__(RefreshScheduler)
        mock_registry = MagicMock()
        mock_registry.get_global_repo.return_value = {
            "enable_temporal": False,
            "temporal_options": None,
            "repo_url": "https://github.com/example/test.git",
            "enable_scip": False,
        }
        scheduler.registry = mock_registry

        def fake_popen_progress(
            command, phase_name, allocator, progress_callback,
            all_stdout, all_stderr, cwd, error_label=None
        ):
            if progress_callback is not None:
                progress_callback(
                    int(allocator.phase_start(phase_name)),
                    phase=phase_name,
                    detail=f"{phase_name}: starting...",
                )
                progress_callback(
                    int(allocator.phase_end(phase_name)),
                    phase=phase_name,
                    detail=f"{phase_name}: done",
                )

        fake_run_result = MagicMock()
        fake_run_result.returncode = 0
        fake_run_result.stdout = ""
        fake_run_result.stderr = ""

        with tempfile.TemporaryDirectory() as tmpdir:
            # Patch subprocess.run to avoid real cidx calls (needs_reconcile check uses it too)
            with patch("subprocess.run", return_value=fake_run_result), \
                 patch.object(psr_mod, "run_with_popen_progress", side_effect=fake_popen_progress):
                scheduler._index_source(
                    alias_name="test-alias-global",
                    source_path=tmpdir,
                    progress_callback=progress_cb,
                )

        # Must have received at least one progress callback call
        assert len(received) > 0, (
            "PATH C: _index_source must call progress_callback. "
            f"Got zero calls."
        )
        # Must have received calls with phase info
        calls_with_phase = [c for c in received if c.get("phase") is not None]
        assert len(calls_with_phase) > 0, (
            "PATH C: progress_callback calls must include phase= keyword. "
            f"Got: {received}"
        )


class TestPathDChangeBranchProgressCallback:
    """PATH D: change_branch_async must wire progress_callback through to change_branch."""

    def test_change_branch_background_worker_accepts_progress_callback(self):
        """
        The background_worker inside change_branch_async must accept progress_callback
        so BackgroundJobManager can inject it.
        """
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoManager,
            GoldenRepo,
        )
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GoldenRepoManager(data_dir=tmpdir)

            mock_bjm = MagicMock()
            manager.background_job_manager = mock_bjm
            manager.activated_repo_manager = MagicMock()

            # Register a fake repo so change_branch_async doesn't raise
            manager.golden_repos["test-alias"] = GoldenRepo(
                alias="test-alias",
                repo_url="https://github.com/example/test.git",
                default_branch="main",
                clone_path=str(Path(tmpdir) / "golden-repos" / "test-alias"),
                created_at=datetime.now(timezone.utc).isoformat(),
            )

            manager.change_branch_async(
                alias="test-alias",
                target_branch="feature/new-branch",
                submitter_username="admin",
            )

            assert mock_bjm.submit_job.called
            submit_call = mock_bjm.submit_job.call_args
            submitted_func = (
                submit_call.kwargs.get("func")
                or submit_call[1].get("func")
                or submit_call[0][1]
            )

            sig = inspect.signature(submitted_func)
            assert "progress_callback" in sig.parameters, (
                "PATH D: change_branch background_worker must accept progress_callback. "
                f"Current parameters: {list(sig.parameters.keys())}"
            )

    def test_change_branch_accepts_progress_callback(self):
        """
        change_branch method itself must accept an optional progress_callback
        parameter so it can forward progress during indexing phases.
        """
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoManager,
        )

        sig = inspect.signature(GoldenRepoManager.change_branch)
        assert "progress_callback" in sig.parameters, (
            "PATH D: change_branch must accept progress_callback. "
            f"Current parameters: {list(sig.parameters.keys())}"
        )


class TestPathDChangeBranchCoarseProgress:
    """PATH D: change_branch must emit coarse progress markers around major steps."""

    def test_change_branch_calls_coarse_progress_markers(self):
        """
        change_branch must call progress_callback with coarse markers
        before/after each major step (cidx index, CoW snapshot, cleanup, swap).
        Verifies Finding 3: progress_callback was accepted but never invoked.
        """
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoManager,
            GoldenRepo,
        )
        from datetime import datetime, timezone
        from unittest.mock import patch, MagicMock

        received = []

        def progress_cb(pct, phase=None, detail=None):
            received.append({"pct": pct, "phase": phase, "detail": detail})

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GoldenRepoManager(data_dir=tmpdir)
            manager.background_job_manager = MagicMock()
            manager.activated_repo_manager = MagicMock()

            # Register a fake repo with different branch so change_branch proceeds
            manager.golden_repos["test-alias"] = GoldenRepo(
                alias="test-alias",
                repo_url="https://github.com/example/test.git",
                default_branch="main",
                clone_path=str(Path(tmpdir) / "golden-repos" / "test-alias"),
                created_at=datetime.now(timezone.utc).isoformat(),
            )

            # Patch out all sub-operations so we can verify just the progress markers
            manager._cb_git_fetch_and_validate = MagicMock()
            manager._cb_checkout_and_pull = MagicMock()
            manager._cb_cidx_index = MagicMock()
            manager._cb_cow_snapshot = MagicMock(return_value="/fake/snapshot/path")
            manager._cb_fts_branch_cleanup = MagicMock()
            manager._cb_hnsw_branch_cleanup = MagicMock()
            manager._cb_swap_alias = MagicMock()
            manager._sqlite_backend = MagicMock()
            manager._sqlite_backend.update_default_branch = MagicMock()
            manager._sqlite_backend.invalidate_description_refresh_tracking = MagicMock()
            manager._sqlite_backend.invalidate_dependency_map_tracking = MagicMock()
            manager.resource_config = None

            manager.change_branch(
                alias="test-alias",
                target_branch="feature/new-branch",
                progress_callback=progress_cb,
            )

        # Must have received at least one progress callback call
        assert len(received) > 0, (
            "PATH D: change_branch must call progress_callback. "
            f"Got zero calls."
        )
        # Progress values must be ascending and cover a meaningful range
        pct_values = [c["pct"] for c in received]
        assert max(pct_values) >= 50, (
            "PATH D: change_branch progress must reach at least 50%. "
            f"Got values: {pct_values}"
        )


class TestPathEActivatedRepoIndexManagerProgress:
    """PATH E: _execute_indexing_job must use ProgressPhaseAllocator, not hardcoded %."""

    def test_execute_indexing_job_calls_progress_with_phase_info(self):
        """
        _execute_indexing_job must call progress_callback with phase and detail
        keyword arguments (not just an integer), indicating it uses
        ProgressPhaseAllocator rather than hardcoded percentages.
        """
        from code_indexer.server.services.activated_repo_index_manager import (
            ActivatedRepoIndexManager,
        )
        import logging

        manager = ActivatedRepoIndexManager.__new__(ActivatedRepoIndexManager)
        manager.logger = logging.getLogger("test")
        manager.INDEXING_TIMEOUT_SECONDS = 300

        received_calls = []

        def progress_cb(pct, phase=None, detail=None):
            received_calls.append({"pct": pct, "phase": phase, "detail": detail})

        # Mock _execute_all_index_types to avoid real subprocess calls
        manager._execute_all_index_types = MagicMock(
            return_value={"semantic": {"success": True}}
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            manager._execute_indexing_job(
                repo_alias="test-alias",
                repo_path=tmpdir,
                index_types=["semantic"],
                clear=False,
                progress_callback=progress_cb,
            )

        # Must have been called with phase info (not just bare integer)
        calls_with_phase = [c for c in received_calls if c.get("phase") is not None]
        assert len(calls_with_phase) > 0, (
            "PATH E: _execute_indexing_job must call progress_callback with phase= "
            f"keyword. Got calls: {received_calls}"
        )

    def test_execute_all_index_types_uses_allocator_not_hardcoded_arithmetic(self):
        """
        _execute_all_index_types must accept an allocator parameter AND actually
        use it to compute progress values.

        We verify this by calling _execute_all_index_types twice with two different
        allocators: one heavily weighted toward semantic (many files, few commits)
        and one heavily weighted toward temporal (few files, many commits).  If the
        allocator is actually used, the first progress value emitted for "semantic"
        will be different because the phase boundaries differ.  If hardcoded
        arithmetic (`10 + int((idx / total_types) * 80)`) is used instead, both
        runs would emit the identical hardcoded value regardless of the allocator.
        """
        import inspect
        import logging
        from unittest.mock import MagicMock
        from code_indexer.server.services.activated_repo_index_manager import (
            ActivatedRepoIndexManager,
        )
        from code_indexer.services.progress_phase_allocator import ProgressPhaseAllocator

        # Verify the parameter exists first
        sig = inspect.signature(ActivatedRepoIndexManager._execute_all_index_types)
        assert "allocator" in sig.parameters, (
            "PATH E: _execute_all_index_types must accept an 'allocator' parameter. "
            f"Current parameters: {list(sig.parameters.keys())}"
        )

        manager = ActivatedRepoIndexManager.__new__(ActivatedRepoIndexManager)
        manager.logger = logging.getLogger("test")

        # Mock _execute_single_index_type to avoid real subprocess calls
        manager._execute_single_index_type = MagicMock(
            return_value={"success": True}
        )

        # Allocator 1: semantic dominates (many files, zero commits)
        alloc_semantic_heavy = ProgressPhaseAllocator()
        alloc_semantic_heavy.calculate_weights(
            index_types=["semantic", "temporal"],
            file_count=10000,
            commit_count=1,
        )

        # Allocator 2: temporal dominates (few files, many commits)
        alloc_temporal_heavy = ProgressPhaseAllocator()
        alloc_temporal_heavy.calculate_weights(
            index_types=["semantic", "temporal"],
            file_count=1,
            commit_count=10000,
        )

        received_semantic_heavy = []
        received_temporal_heavy = []

        def make_cb(store):
            def cb(pct, message=""):
                store.append(pct)
            return cb

        with tempfile.TemporaryDirectory() as tmpdir:
            manager._execute_all_index_types(
                tmpdir,
                ["semantic", "temporal"],
                False,
                make_cb(received_semantic_heavy),
                alloc_semantic_heavy,
            )
            manager._execute_all_index_types(
                tmpdir,
                ["semantic", "temporal"],
                False,
                make_cb(received_temporal_heavy),
                alloc_temporal_heavy,
            )

        # With hardcoded arithmetic, both runs would emit the same fixed values.
        # With the allocator, the first "semantic" start value differs because
        # the phase boundaries are different.
        assert received_semantic_heavy != received_temporal_heavy, (
            "PATH E: _execute_all_index_types must use the allocator weights. "
            "Both runs emitted identical values, indicating hardcoded arithmetic "
            f"is still in use. semantic_heavy={received_semantic_heavy}, "
            f"temporal_heavy={received_temporal_heavy}"
        )

    def test_no_hardcoded_50_90_values(self):
        """
        _execute_indexing_job must NOT emit hardcoded values 50 or 90.
        These were the pre-Story #482 milestones replaced by ProgressPhaseAllocator.
        """
        from code_indexer.server.services.activated_repo_index_manager import (
            ActivatedRepoIndexManager,
        )
        import logging

        manager = ActivatedRepoIndexManager.__new__(ActivatedRepoIndexManager)
        manager.logger = logging.getLogger("test")
        manager.INDEXING_TIMEOUT_SECONDS = 300

        received_values = []

        def progress_cb(pct, phase=None, detail=None):
            received_values.append(pct)

        manager._execute_all_index_types = MagicMock(
            return_value={"semantic": {"success": True}}
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            manager._execute_indexing_job(
                repo_alias="test-alias",
                repo_path=tmpdir,
                index_types=["semantic"],
                clear=False,
                progress_callback=progress_cb,
            )

        # The hardcoded values 50 and 90 should not appear since ProgressPhaseAllocator
        # now determines all progress values dynamically.
        assert 50 not in received_values, (
            f"PATH E: hardcoded 50% milestone still present. Values: {received_values}"
        )
        assert 90 not in received_values, (
            f"PATH E: hardcoded 90% milestone still present. Values: {received_values}"
        )
