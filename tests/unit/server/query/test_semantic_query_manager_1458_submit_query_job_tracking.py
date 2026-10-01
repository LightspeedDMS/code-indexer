"""submit_query_job()'s background-job execution must be QueryTracker
refcount-tracked (Story #1458 Codex HIGH finding, round 2).

Codex's finding: MCP (_search_activated_repo), REST (inline_query.py), and
wiki (user_wiki_search) all wrap their SYNCHRONOUS/inline calls to
query_user_repositories() with track_activated_repo_query() -- but
submit_query_job() hands the bare query_user_repositories method straight to
BackgroundJobManager.submit_job(), which invokes it later on a worker thread
via func(*args, **kwargs), completely outside any of those three wrappers.
A background-job-driven query is therefore invisible to deactivation's
bounded refcount drain, which could purge an activated repo's chunks.db
mid-query.

Real QueryTracker (not mocked) -- only the BackgroundJobManager collaborator
is a lightweight fake that captures the submitted callable so the test can
invoke it exactly the way the real background worker does
(func(*args, **kwargs), per background_jobs.py's _execute_job).
"""

from pathlib import Path
from typing import Any, Callable, Dict, Optional
from unittest.mock import MagicMock

from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.server.query.semantic_query_manager import SemanticQueryManager


class _CapturingBackgroundJobManager:
    """Captures the func actually submitted to submit_job() so the test can
    invoke it later, mirroring background_jobs.py's real func(**kwargs)
    dispatch on a worker thread."""

    def __init__(self) -> None:
        self.captured_func: Optional[Callable[..., Any]] = None
        self.captured_kwargs: Dict[str, Any] = {}

    def submit_job(self, operation_type, func, *args, **kwargs):
        self.captured_func = func
        # Mirror background_jobs.py: submitter_username/repo_alias are
        # bookkeeping-only, consumed by submit_job itself, never forwarded
        # to func.
        self.captured_kwargs = {
            k: v
            for k, v in kwargs.items()
            if k not in ("submitter_username", "repo_alias")
        }
        return "job-123"


class TestSubmitQueryJobRefcountTracking:
    def test_background_job_execution_is_refcount_tracked(self, tmp_path: Path) -> None:
        real_query_tracker = QueryTracker()
        activated_repos_dir = str(tmp_path / "activated-repos")
        expected_key = f"{activated_repos_dir}/testuser/my-repo"

        activated_repo_manager = MagicMock()
        activated_repo_manager.activated_repos_dir = activated_repos_dir

        job_manager = _CapturingBackgroundJobManager()
        manager = SemanticQueryManager(
            data_dir=str(tmp_path),
            activated_repo_manager=activated_repo_manager,
            background_job_manager=job_manager,  # type: ignore[arg-type]
        )
        manager.set_query_tracker(real_query_tracker)

        observed_refcount_during_call = {}

        def fake_query_user_repositories(**kwargs):
            observed_refcount_during_call["value"] = real_query_tracker.get_ref_count(
                expected_key
            )
            return {
                "results": [],
                "total_results": 0,
                "query_metadata": {},
            }

        manager.query_user_repositories = fake_query_user_repositories  # type: ignore[method-assign]

        job_id = manager.submit_query_job(
            username="testuser",
            query_text="test",
            repository_alias="my-repo",
        )

        assert job_id == "job-123"
        assert job_manager.captured_func is not None

        # Invoke exactly the way the REAL background worker does.
        job_manager.captured_func(**job_manager.captured_kwargs)

        # Refcount was >0 WHILE the background job executed the query...
        assert observed_refcount_during_call.get("value") == 1
        # ...and is back to 0 after it completes.
        assert real_query_tracker.get_ref_count(expected_key) == 0

    def test_noop_when_repository_alias_is_none(self, tmp_path: Path) -> None:
        """Omni/cross-repo background queries (no single repository_alias)
        must remain a true no-op, preserving today's behavior exactly."""
        real_query_tracker = QueryTracker()
        activated_repo_manager = MagicMock()
        activated_repo_manager.activated_repos_dir = str(tmp_path / "activated-repos")

        job_manager = _CapturingBackgroundJobManager()
        manager = SemanticQueryManager(
            data_dir=str(tmp_path),
            activated_repo_manager=activated_repo_manager,
            background_job_manager=job_manager,  # type: ignore[arg-type]
        )
        manager.set_query_tracker(real_query_tracker)

        def fake_query_user_repositories(**kwargs):
            return {"results": [], "total_results": 0, "query_metadata": {}}

        manager.query_user_repositories = fake_query_user_repositories  # type: ignore[method-assign]

        manager.submit_query_job(
            username="testuser",
            query_text="test",
            repository_alias=None,
        )
        assert job_manager.captured_func is not None
        job_manager.captured_func(**job_manager.captured_kwargs)

        assert real_query_tracker.get_all_paths() == set()
