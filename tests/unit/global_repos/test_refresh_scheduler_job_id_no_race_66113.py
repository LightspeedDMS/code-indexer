"""EVO-66113 MR !53 review fix (MAJOR finding, discussion
4defdd944af7679fdfaa797817d324f819ffe82f): prove job_id reaches
RefreshScheduler's _submit_refresh_job/_refresh_worker closure AND the
downstream _index_source() embed-stats env wiring WITHOUT relying on the
removed `_job_id_holder` mutable-holder pattern.

Companion regression to
tests/unit/server/repositories/test_golden_repo_manager_job_id_no_race_66113.py
(same MR, same race). The holder dict was only populated with the real
job_id AFTER submit_job() returned, but submit_job() enqueues the
(job_id, func, args, kwargs) tuple onto a queue.Queue BEFORE returning -- a
worker thread could dequeue and start running the closure before the
holder write happened. The fix removes the holder: `_refresh_worker`
declares a plain `job_id=None` parameter, and
BackgroundJobManager._execute_job's signature-based `enhanced_kwargs`
injection supplies the real job_id atomically as part of the SAME
dequeued call -- no separate write, no race.

This file also exercises a second, deeper bug uncovered while writing this
regression test: `_index_source()`'s nested `_run_popen_c` closure
referenced a bare `job_id` name that was never a parameter of
`_index_source()` and never assigned locally anywhere in its scope --
every call reaching `_run_popen_c()` raised `NameError: name 'job_id' is
not defined`, unconditionally, regardless of whether the caller intended
to pass a job_id. Fixed by adding `job_id: Optional[str] = None` to
`_index_source()`'s signature and forwarding `job_id=job_id` from its sole
production call site in `_execute_refresh()`.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, Mock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.config import ConfigManager
from code_indexer.server.utils.config_manager import ServerConfig


@pytest.fixture
def golden_repos_dir(tmp_path):
    grd = tmp_path / "golden_repos"
    grd.mkdir(parents=True)
    return grd


@pytest.fixture
def config_mgr(tmp_path):
    return ConfigManager(tmp_path / ".code-indexer" / "config.json")


@pytest.fixture
def query_tracker():
    return QueryTracker()


@pytest.fixture
def cleanup_manager(query_tracker):
    return CleanupManager(query_tracker)


@pytest.fixture
def mock_registry():
    registry = MagicMock()
    registry.get_global_repo.return_value = {
        "alias": "test-repo-global",
        "repo_url": "git@github.com:org/repo.git",
        "enable_temporal": True,
        "temporal_options": None,
        "enable_scip": False,
    }
    return registry


@pytest.fixture
def scheduler(
    golden_repos_dir, config_mgr, query_tracker, cleanup_manager, mock_registry
):
    sched = RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        registry=mock_registry,
    )
    return sched


@pytest.fixture
def source_repo(tmp_path):
    src = tmp_path / "source_repo"
    src.mkdir()
    (src / "README.md").write_text("# Test Repo")
    (src / ".git").mkdir()
    return src


class TestSubmitRefreshJobClosureThreadsJobIdWithoutHolder:
    def test_closure_declares_job_id_directly_not_via_holder(self, scheduler):
        """Regression guard for the racy pattern itself: the closure must
        accept job_id as a normal parameter (so BackgroundJobManager's
        inspect.signature()-based injection finds it), not read it from a
        mutable holder populated after submit_job() returns."""
        captured: list = []

        def _capture(operation_type, func, submitter_username, **kwargs):
            captured.append(func)
            return "submitted-refresh-job-id"

        scheduler.background_job_manager = Mock()
        scheduler.background_job_manager.submit_job.side_effect = _capture

        scheduler._submit_refresh_job("test-repo-global")

        assert len(captured) == 1
        worker = captured[0]
        assert "job_id" in inspect.signature(worker).parameters

    def test_closure_forwards_explicit_job_id_to_execute_refresh(self, scheduler):
        captured: list = []

        def _capture(operation_type, func, submitter_username, **kwargs):
            captured.append(func)
            return "submitted-refresh-job-id"

        scheduler.background_job_manager = Mock()
        scheduler.background_job_manager.submit_job.side_effect = _capture
        scheduler._execute_refresh = Mock(return_value={"success": True})  # type: ignore[method-assign]

        scheduler._submit_refresh_job("test-repo-global")

        worker = captured[0]
        # Simulate BackgroundJobManager._execute_job's auto-injection: the
        # real job_id arrives as a kwarg on the SAME call that runs the
        # closure -- not via a separately-populated mutable holder.
        worker(progress_callback=None, job_id="race-test-refresh-job-42")

        assert (
            scheduler._execute_refresh.call_args.kwargs["job_id"]
            == "race-test-refresh-job-42"
        )

    def test_closure_forwards_none_when_no_job_id_injected(self, scheduler):
        """CLI/solo-mode-like invocation with no job_id kwarg at all must not
        raise -- job_id stays optional."""
        captured: list = []

        def _capture(operation_type, func, submitter_username, **kwargs):
            captured.append(func)
            return "submitted-refresh-job-id"

        scheduler.background_job_manager = Mock()
        scheduler.background_job_manager.submit_job.side_effect = _capture
        scheduler._execute_refresh = Mock(return_value={"success": True})  # type: ignore[method-assign]

        scheduler._submit_refresh_job("test-repo-global")

        worker = captured[0]
        worker()

        assert scheduler._execute_refresh.call_args.kwargs["job_id"] is None


def _make_fake_run_with_popen_progress(calls):
    def _fake(*, command, phase_name, env=None, **kwargs):
        calls.append({"phase_name": phase_name, "env": env})
        return 100

    return _fake


class TestIndexSourceThreadsJobIdIntoEmbedStatsEnvWithoutNameError:
    """Regression guard for the NameError bug found while writing this test:
    `_index_source()`'s nested `_run_popen_c` closure referenced a bare
    `job_id` free variable that was never a parameter of `_index_source()`
    and never locally assigned -- every call that reached `_run_popen_c()`
    raised `NameError: name 'job_id' is not defined`, unconditionally.
    """

    def test_explicit_job_id_reaches_semantic_and_temporal_embed_stats_env(
        self, scheduler, source_repo
    ):
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            EMBEDDING_STATS_JOB_ID_ENV,
        )

        server_config = ServerConfig(
            server_dir="/opt/cidx-server", storage_mode="sqlite"
        )

        calls: list = []
        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=_make_fake_run_with_popen_progress(calls),
            ),
            patch(
                "code_indexer.global_repos.refresh_scheduler.get_config_service"
            ) as mock_get_cfg_svc,
        ):
            mock_get_cfg_svc.return_value.get_config.return_value = server_config

            scheduler._index_source(
                alias_name="test-repo-global",
                source_path=str(source_repo),
                job_id="race-test-refresh-job-env-1",
            )

        by_phase = {c["phase_name"]: c["env"] for c in calls}
        assert "semantic" in by_phase, f"expected a semantic-phase call, got: {calls}"
        semantic_env = by_phase["semantic"]
        assert semantic_env is not None
        assert (
            semantic_env[EMBEDDING_STATS_JOB_ID_ENV] == "race-test-refresh-job-env-1"
        ), (
            "EVO-66113 review fix regression: job_id passed to _index_source() "
            "must reach the semantic-phase child cidx index subprocess env "
            "for embed-stats correlation"
        )

        if "temporal" in by_phase:
            temporal_env = by_phase["temporal"]
            assert temporal_env is not None
            assert (
                temporal_env[EMBEDDING_STATS_JOB_ID_ENV]
                == "race-test-refresh-job-env-1"
            ), (
                "EVO-66113 review fix regression: job_id must also reach the "
                "temporal-phase child cidx index subprocess env -- both "
                "phases share the same _run_popen_c convergence point"
            )

    def test_no_job_id_kwarg_does_not_raise_name_error(self, scheduler, source_repo):
        """Pre-fix, this call raised NameError even without a job_id kwarg at
        all -- `_index_source()` never bound the name `job_id` anywhere in
        its own scope, so the nested `_run_popen_c` closure's free-variable
        reference was unconditionally unresolved regardless of caller intent.
        """
        server_config = ServerConfig(
            server_dir="/opt/cidx-server", storage_mode="sqlite"
        )

        calls: list = []
        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=_make_fake_run_with_popen_progress(calls),
            ),
            patch(
                "code_indexer.global_repos.refresh_scheduler.get_config_service"
            ) as mock_get_cfg_svc,
        ):
            mock_get_cfg_svc.return_value.get_config.return_value = server_config

            # Must not raise NameError.
            scheduler._index_source(
                alias_name="test-repo-global",
                source_path=str(source_repo),
            )

        by_phase = {c["phase_name"]: c["env"] for c in calls}
        assert by_phase["semantic"] is not None
