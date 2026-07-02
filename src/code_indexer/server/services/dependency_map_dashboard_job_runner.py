"""
DependencyMapDashboardJobRunner for Story #684 Phase 3.

Runs dependency map dashboard analysis as a background job,
reporting progress to a job tracker and caching the final result.
"""

import json
import logging

logger = logging.getLogger(__name__)

_PCT_COMPLETE = 100
_PCT_ZERO = 0


class DependencyMapDashboardJobRunner:
    """Runs dep-map dashboard analysis as a background job with progress reporting."""

    def __init__(self, cache_backend, dashboard_service, job_tracker) -> None:
        """
        Initialize the runner.

        Args:
            cache_backend: DependencyMapDashboardCacheBackend instance.
            dashboard_service: DependencyMapDashboardService instance.
            job_tracker: JobTracker (or compatible) with update_status(job_id, **kwargs).

        Raises:
            ValueError: If any argument is None.
        """
        if cache_backend is None:
            raise ValueError("cache_backend must not be None")
        if dashboard_service is None:
            raise ValueError("dashboard_service must not be None")
        if job_tracker is None:
            raise ValueError("job_tracker must not be None")
        self._cache = cache_backend
        self._dashboard = dashboard_service
        self._tracker = job_tracker

    def run(self, job_id: str) -> None:
        """
        Execute the analysis job, reporting progress and caching results.

        Reports status='running' at start, fires progress callbacks during
        execution, caches the result and reports status='completed' on
        success, or marks cache failure and reports status='failed' on error.

        Args:
            job_id: Identifier for this job (used for tracker updates).

        Raises:
            ValueError: If job_id is None or empty.
            Exception: Re-raises any exception raised by dashboard_service.get_job_status().
        """
        if not job_id:
            raise ValueError("job_id must not be None or empty")

        self._tracker.update_status(job_id, status="running")
        try:

            def callback(done: int, total: int) -> None:
                pct = (
                    int(done * _PCT_COMPLETE / total)
                    if total > _PCT_ZERO
                    else _PCT_ZERO
                )
                self._tracker.update_status(
                    job_id,
                    status="running",
                    progress=pct,
                    progress_info=f"{done}/{total}",
                )

            result = self._dashboard.get_job_status(progress_callback=callback)
            self._cache.set_cached(json.dumps(result))
            self._tracker.update_status(
                job_id, status="completed", progress=_PCT_COMPLETE
            )
        except Exception as e:
            logger.error("Dashboard job %s failed: %s", job_id, e)
            self._cache.mark_job_failed(str(e))
            self._tracker.update_status(job_id, status="failed", error=str(e))
            raise
