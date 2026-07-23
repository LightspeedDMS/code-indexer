"""
Unit tests for Bug #1452 (3rd follow-up): web/routes.py's `_apply_job_filters`
helper search predicate must also match job_id.

`_apply_job_filters` is applied to JobTracker-only jobs (e.g.
dependency_map_full / dependency_map_delta) merged into the /admin/jobs
display alongside BackgroundJobManager jobs (see `_get_all_jobs`'s
docstring, Bug #736). It mirrors the same repo_alias/username/
operation_type/error search semantics as `BackgroundJobManager.
get_jobs_for_display` and `list_jobs_filtered` -- but, like those two, never
matched job_id, so clicking a job's own "Job ID: <link>"
(`/admin/jobs?search=<job_id>`) could still miss a JobTracker-only job even
after the two prior param-name fixes.
"""

from code_indexer.server.web.routes import _apply_job_filters


def _job(
    job_id, repo_alias="my-repo", username="alice", operation_type="sync", error=None
):
    return {
        "job_id": job_id,
        "repo_alias": repo_alias,
        "username": username,
        "operation_type": operation_type,
        "error": error,
    }


class TestApplyJobFiltersJobIdSearch:
    """Searching by job_id must return the matching job dict."""

    def test_search_by_exact_job_id_returns_matching_job(self):
        target_job_id = "9740fda1-102e-4213-875b-c6124e1b62b2"
        jobs = [_job(target_job_id), _job("some-other-job-id")]

        result = _apply_job_filters(
            jobs, status_filter=None, type_filter=None, search=target_job_id
        )

        result_ids = [j["job_id"] for j in result]
        assert target_job_id in result_ids, (
            f"Expected job_id search to find {target_job_id}, got: {result_ids}"
        )
        assert len(result) == 1

    def test_search_by_partial_job_id_substring_returns_matching_job(self):
        target_job_id = "9740fda1-102e-4213-875b-c6124e1b62b2"
        jobs = [_job(target_job_id), _job("unrelated-job-id")]

        result = _apply_job_filters(
            jobs, status_filter=None, type_filter=None, search="102e-4213"
        )

        result_ids = [j["job_id"] for j in result]
        assert target_job_id in result_ids
        assert len(result) == 1
