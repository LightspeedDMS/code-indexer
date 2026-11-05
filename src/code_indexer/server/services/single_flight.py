"""A cluster-wide single-flight claim for a periodic background pass.

The claim is a job-tracker row on a sentinel alias: the tracker's unique
active-job index lets exactly one pod hold it (``register_job_if_no_conflict``
raises ``DuplicateJobError`` for every other). Without a tracker (solo/CLI)
nothing else can run, and the claim is None.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional


def claim(
    job_tracker: Optional[Any], operation: str, sentinel_alias: str, submitter: str
) -> Optional[str]:
    """The claim's job id (None: no tracker). Raises ``DuplicateJobError``
    when another pod holds it."""
    if job_tracker is None:
        return None
    job_id = uuid.uuid4().hex
    job_tracker.register_job_if_no_conflict(
        job_id=job_id,
        operation_type=operation,
        username=submitter,
        repo_alias=sentinel_alias,
    )
    return job_id


def release(
    job_tracker: Optional[Any],
    job_id: Optional[str],
    *,
    result: Optional[Any] = None,
    error: Optional[str] = None,
) -> None:
    """Finish the claim: failed with *error*, else completed with *result*."""
    if job_tracker is None or job_id is None:
        return
    if error is not None:
        job_tracker.fail_job(job_id, error=error)
    else:
        job_tracker.complete_job(job_id, result=result)
