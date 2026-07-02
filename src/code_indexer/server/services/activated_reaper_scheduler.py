"""
Activated Repository Reaper Scheduler (Story #967).

Background daemon thread that submits a 'reap_activated_repos' background job
at a configurable cadence. Cadence is re-read from config on each cycle so
that Web UI changes take effect without a server restart.
"""

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Granularity of the sleep loop: check stop_event this often (seconds).
_TICK_SECONDS = 60

# Default cadence used when config cannot be read (safe fallback).
_DEFAULT_CADENCE_HOURS = 24

# Conversion factor for hours -> seconds.
_SECONDS_PER_HOUR = 3600


class ActivatedReaperScheduler:
    """
    Daemon scheduler that periodically submits reap-activated-repos jobs.

    The scheduler submits a BackgroundJobManager job on each tick so that
    cycles appear in the job dashboard (AC3).  Cadence is re-read from
    config_service on every cycle so Web UI changes are honoured without
    a restart (AC4).
    """

    def __init__(
        self,
        service: Any,
        background_job_manager: Any,
        config_service: Any,
    ) -> None:
        """
        Initialise the scheduler.

        Args:
            service:                ActivatedReaperService with run_reap_cycle().
            background_job_manager: BackgroundJobManager with submit_job().
            config_service:         ConfigService with get_config() returning ServerConfig.
        """
        self._service = service
        self._background_job_manager = background_job_manager
        self._config_service = config_service

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the daemon thread.  A reap job is submitted on the first tick."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="ActivatedReaperScheduler",
        )
        self._thread.start()
        logger.info("ActivatedReaperScheduler started")

    def stop(self) -> None:
        """Signal the scheduler to stop and wait for the thread to finish."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("ActivatedReaperScheduler stopped")

    # ------------------------------------------------------------------
    # Manual trigger
    # ------------------------------------------------------------------

    def trigger_now(self) -> str:
        """
        Submit a reap cycle job immediately, independent of the cadence timer.

        Returns:
            job_id returned by background_job_manager.submit_job().
        """
        job_id: str = self._background_job_manager.submit_job(
            "reap_activated_repos",
            self._service.run_reap_cycle,
            submitter_username="system",
            is_admin=True,
            repo_alias="server",
        )
        logger.info(
            "ActivatedReaperScheduler: triggered reap cycle (job_id=%s)", job_id
        )
        return job_id

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Main loop: submit a reap job, then wait for the configured cadence."""
        while not self._stop_event.is_set():
            try:
                self.trigger_now()
            except Exception as exc:
                logger.error(
                    "ActivatedReaperScheduler: error submitting reap job: %s",
                    exc,
                    exc_info=True,
                )

            # Re-read cadence from config each cycle (AC4).
            try:
                cadence_hours = self._config_service.get_config().activated_reaper_config.cadence_hours
            except Exception as exc:
                logger.warning(
                    "ActivatedReaperScheduler: failed to read cadence from config, "
                    "falling back to %d hours: %s",
                    _DEFAULT_CADENCE_HOURS,
                    exc,
                    exc_info=True,
                )
                cadence_hours = _DEFAULT_CADENCE_HOURS

            cadence_seconds = cadence_hours * _SECONDS_PER_HOUR
            elapsed = 0
            while elapsed < cadence_seconds and not self._stop_event.is_set():
                self._stop_event.wait(timeout=_TICK_SECONDS)
                elapsed += _TICK_SECONDS
