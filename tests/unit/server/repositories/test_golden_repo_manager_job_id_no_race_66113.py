"""EVO-66113 MR !53 review fix (MAJOR finding, discussion
6ac7a6ff9cf5f6247902b955b62375bfe3f02369): prove job_id reaches the
background_worker closures for add_golden_repo and
add_indexes_to_golden_repo WITHOUT relying on the removed
`_job_id_holder` mutable-holder pattern.

The review flagged the original fix as racy: the holder dict was only
populated with the real job_id AFTER submit_job() returned, but
submit_job() enqueues the (job_id, func, args, kwargs) tuple onto a
queue.Queue BEFORE returning -- a worker thread could dequeue and start
running the closure before the holder write happened, so the closure
could read the stale holder value (None) instead of the real job_id.

The fix removes the holder: each closure now declares a plain
`job_id=None` parameter, and BackgroundJobManager._execute_job's
signature-based `enhanced_kwargs` injection (background_jobs.py, "Story
#1400 CRITICAL 2") supplies the real job_id atomically as part of the
SAME dequeued call -- no separate write, no race.

These tests call each captured closure directly with an explicit job_id
(exactly what _execute_job's auto-injection does) and assert it reaches
the downstream consumer that uses it for embed-stats correlation.
"""

from __future__ import annotations

import inspect
from unittest.mock import Mock, patch

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.utils.config_manager import ServerConfig


def _manager_for_add_golden_repo(tmp_path) -> GoldenRepoManager:
    m = GoldenRepoManager(data_dir=str(tmp_path))
    m._git_backend = Mock()
    m._git_backend.owns_credentials = False
    captured: list = []

    def _capture(operation_type, func, submitter_username, **kwargs):
        captured.append(func)
        return "submitted-job-id"

    m.background_job_manager = Mock()
    m.background_job_manager.submit_job.side_effect = _capture
    m._captured_workers = captured
    m._validate_git_repository = Mock(return_value=True)  # type: ignore[method-assign]
    m.execute_add_golden_repo_work = Mock(return_value={"success": True})  # type: ignore[method-assign]
    return m


class TestAddGoldenRepoJobIdReachesExecuteWorkWithoutHolder:
    def test_closure_declares_job_id_directly_not_via_holder(self, tmp_path):
        """Regression guard for the racy pattern itself: the closure must
        accept job_id as a normal parameter (so BackgroundJobManager's
        inspect.signature()-based injection finds it), not read it from a
        mutable holder populated after submit_job() returns."""
        m = _manager_for_add_golden_repo(tmp_path)

        m.add_golden_repo(repo_url="https://example.com/repo.git", alias="race-fix-1")

        assert len(m._captured_workers) == 1
        worker = m._captured_workers[0]
        assert "job_id" in inspect.signature(worker).parameters

    def test_closure_forwards_explicit_job_id_to_execute_work(self, tmp_path):
        m = _manager_for_add_golden_repo(tmp_path)

        m.add_golden_repo(repo_url="https://example.com/repo.git", alias="race-fix-2")

        worker = m._captured_workers[0]
        # Simulate BackgroundJobManager._execute_job's auto-injection: the
        # real job_id arrives as a kwarg on the SAME call that runs the
        # closure -- not via a separately-populated mutable holder.
        worker(progress_callback=None, job_id="race-test-job-77")

        assert (
            m.execute_add_golden_repo_work.call_args.kwargs["job_id"]
            == "race-test-job-77"
        )

    def test_closure_forwards_none_when_no_job_id_injected(self, tmp_path):
        """CLI/solo-mode-like invocation with no job_id kwarg at all must not
        raise -- job_id stays optional."""
        m = _manager_for_add_golden_repo(tmp_path)

        m.add_golden_repo(repo_url="https://example.com/repo.git", alias="race-fix-3")

        worker = m._captured_workers[0]
        worker()

        assert m.execute_add_golden_repo_work.call_args.kwargs["job_id"] is None


def _make_add_indexes_manager(tmp_path, temporal_options=None):
    """Build a minimal GoldenRepoManager with a real repo_path on disk.

    Mirrors test_golden_repo_manager_add_indexes_temporal_pg_env_wiring_1313.py's
    construction style.
    """
    with patch.object(GoldenRepoManager, "__init__", lambda self, *a, **kw: None):
        manager = GoldenRepoManager.__new__(GoldenRepoManager)

    repo_path = tmp_path / "golden-repos" / "test-repo"
    (repo_path / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)

    manager.data_dir = str(tmp_path)
    manager.golden_repos_dir = str(tmp_path / "golden-repos")

    golden_repo = Mock()
    golden_repo.alias = "test-repo"
    golden_repo.clone_path = str(repo_path)
    golden_repo.temporal_options = temporal_options or {}
    golden_repo.enable_temporal = False

    manager.golden_repos = {"test-repo": golden_repo}
    manager.get_actual_repo_path = Mock(return_value=str(repo_path))
    manager._sqlite_backend = Mock()
    manager._sqlite_backend.update_enable_temporal = Mock(return_value=True)
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
    manager._global_repos_backend = Mock()

    captured_workers: list = []

    def capture_and_run(operation_type, func, submitter_username, **kwargs):
        captured_workers.append(func)
        return "job-add-indexes-test"

    manager.background_job_manager = Mock()
    manager.background_job_manager.submit_job.side_effect = capture_and_run
    manager._captured_workers = captured_workers
    manager._refresh_scheduler = None

    return manager, repo_path


def _mock_subprocess_run(command, **kwargs):
    return Mock(returncode=0, stdout="", stderr="")


class TestAddIndexesToGoldenRepoJobIdReachesEmbedStatsEnvWithoutHolder:
    def test_closure_declares_job_id_directly_not_via_holder(self, tmp_path):
        manager, _ = _make_add_indexes_manager(tmp_path)

        manager.add_indexes_to_golden_repo(alias="test-repo", index_types=["semantic"])

        assert len(manager._captured_workers) == 1
        worker = manager._captured_workers[0]
        assert "job_id" in inspect.signature(worker).parameters

    def test_explicit_job_id_reaches_embedding_stats_child_env(self, tmp_path):
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            EMBEDDING_STATS_JOB_ID_ENV,
        )

        manager, _ = _make_add_indexes_manager(tmp_path)
        server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")

        calls: list = []

        def _fake_run_with_popen_progress(*, command, phase_name, env=None, **kwargs):
            calls.append({"phase_name": phase_name, "env": env})
            return 100

        with (
            patch(
                "code_indexer.server.repositories.golden_repo_manager.subprocess.run",
                side_effect=_mock_subprocess_run,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=_fake_run_with_popen_progress,
            ),
            patch(
                "code_indexer.server.services.config_service.get_config_service"
            ) as mock_get_cfg_svc,
        ):
            mock_get_cfg_svc.return_value.get_config.return_value = server_config

            manager.add_indexes_to_golden_repo(
                alias="test-repo", index_types=["semantic"]
            )
            assert len(manager._captured_workers) == 1
            # Simulate BackgroundJobManager._execute_job's auto-injection.
            manager._captured_workers[0](
                progress_callback=None, job_id="race-test-job-99"
            )

        by_phase = {c["phase_name"]: c["env"] for c in calls}
        semantic_env = by_phase["semantic"]
        assert semantic_env is not None
        assert semantic_env[EMBEDDING_STATS_JOB_ID_ENV] == "race-test-job-99", (
            "EVO-66113 review fix regression: job_id passed to the closure "
            "must reach the child cidx index subprocess env for embed-stats "
            "correlation -- this is exactly what the removed racy "
            "_job_id_holder pattern was trying (unsafely) to achieve"
        )
