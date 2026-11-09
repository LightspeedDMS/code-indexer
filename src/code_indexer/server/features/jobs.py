"""Consume ``background_jobs()``: feature jobs in the core's job manager.

Every ``BackgroundJob`` a loaded feature registers runs as a
BackgroundJobManager job, so it is listed in the dashboard and the job APIs
like any core job:

* on demand -- the feature calls ``run_registered(name)`` on its
  ``background_jobs`` capability (from a tool, a route, a hook);
* recurring -- ``interval_seconds`` set: :class:`FeatureJobScheduler` submits
  it once per interval, first after one interval (never at boot).

Both paths submit with a feature-namespaced dedup key, so the job tracker's
active-job index lets ONE run be active across the whole cluster: every node
ticks, the first claim wins, the others skip that tick.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Mapping, Optional

from cidx_sdk import BackgroundJob
from cidx_sdk.capabilities import BACKGROUND_JOBS

logger = logging.getLogger(__name__)

#: Submitter recorded on scheduled feature jobs.
SCHEDULER_SUBMITTER = "system"
#: How long :meth:`FeatureJobScheduler.stop` waits for each ticker thread.
_STOP_JOIN_SECONDS = 5.0


def describe_jobs(jobs: Any) -> List[Dict[str, Any]]:
    """JSON-friendly listing of a feature's registered jobs (health block)."""
    return [
        {
            "name": job.name,
            "operation_type": job.operation_type,
            "interval_seconds": job.interval_seconds,
        }
        for job in jobs
    ]


class FeatureJobScheduler:
    """One daemon ticker thread per recurring feature job."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

    def recurring(self) -> List[Mapping[str, Any]]:
        """``{"feature", "job"}`` for every recurring job."""
        return [
            {"feature": item.manifest.name, "job": job}
            for item in self._registry.loaded
            for job in item.contributions.background_jobs
            if job.interval_seconds is not None
        ]

    def start(self) -> None:
        """Start the tickers (idempotent)."""
        if self._threads:
            return
        for entry in self.recurring():
            thread = threading.Thread(
                target=self._tick_loop,
                args=(entry["feature"], entry["job"]),
                name=f"feature-job-{entry['feature']}-{entry['job'].name}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def stop(self) -> None:
        """Stop the tickers; a job already submitted finishes in the manager."""
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=_STOP_JOIN_SECONDS)
        self._threads = []

    def _tick_loop(self, feature: str, job: BackgroundJob) -> None:
        interval = float(job.interval_seconds or 0)
        while not self._stop.wait(interval):
            self.tick(feature, job)

    def tick(self, feature: str, job: BackgroundJob) -> Optional[str]:
        """Submit *job* once; None when another node already runs it or the
        submission failed (logged, retried next tick)."""
        ctx = self._registry.context_for(feature)
        if not ctx.has_capability(BACKGROUND_JOBS):
            logger.warning(
                "feature %s job %s not scheduled: no background_jobs capability",
                feature,
                job.name,
            )
            return None
        from code_indexer.server.features.capabilities import JobAlreadyActiveError

        jobs = ctx.capability(BACKGROUND_JOBS)
        try:
            job_id: str = jobs.run_registered(job.name, submitter=SCHEDULER_SUBMITTER)
        except JobAlreadyActiveError as exc:
            logger.debug(
                "feature %s job %s skipped this tick: %s", feature, job.name, exc
            )
            return None
        except Exception as exc:
            logger.warning(
                "feature %s job %s submission failed (%s)",
                feature,
                job.name,
                type(exc).__name__,
            )
            return None
        return job_id
