"""Shared test double for the background job runner.

:class:`RecordingJobManager` records every job submission and returns a job
id without running the job's work. Use it where a test asserts WHETHER (and
what) a route or service submitted, never what the job then does -- the
decision under test belongs to the submission, and running the real clone,
sync or index work would only add incidental time and side effects.
"""

from __future__ import annotations

from typing import Any, Dict, List


class RecordingJobManager:
    """Test double for the background job runner (records submissions)."""

    def __init__(self) -> None:
        self.submissions: List[Dict[str, Any]] = []

    def submit_job(self, operation_type: str, func: Any, *args: Any, **kwargs: Any):
        job_id = f"job-{len(self.submissions) + 1:04d}"
        self.submissions.append(
            {"operation_type": operation_type, "job_id": job_id, **kwargs}
        )
        return job_id

    def get_jobs_by_operation_and_params(self, **_filters: Any) -> List[Any]:
        """No job is ever running (nothing is executed)."""
        return []
