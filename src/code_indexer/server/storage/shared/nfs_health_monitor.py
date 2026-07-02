"""NFS mount health monitor for ONTAP FSx shared storage.

Runs a background thread that periodically calls NfsMountValidator and caches
the last health result.  Callers read ``is_healthy`` to decide whether the node
should accept work.

Anti-fallback policy: an unhealthy NFS mount means the node is DOWN.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .nfs_validator import NfsMountValidator

logger = logging.getLogger(__name__)


class NfsHealthMonitor:
    """Background thread monitoring NFS mount health.

    Usage::

        validator = NfsMountValidator("/mnt/fsx")
        monitor = NfsHealthMonitor(validator, check_interval=30)
        monitor.start()

        # Later…
        if not monitor.is_healthy:
            raise RuntimeError("NFS mount is DOWN — refusing to accept work")

        monitor.stop()
    """

    def __init__(
        self,
        validator: NfsMountValidator,
        check_interval: int = 30,
    ) -> None:
        self._validator = validator
        self._check_interval = check_interval
        self._healthy: bool = True
        self._last_check: dict = {}
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start background monitoring thread.

        Performs an immediate health check before spawning the thread so that
        ``is_healthy`` reflects reality from the first call.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning(
                "NfsHealthMonitor.start() called but monitor is already running"
            )
            return

        self._stop_event.clear()

        # Initial synchronous check so callers get an immediate reading
        self._run_check()

        self._thread = threading.Thread(
            target=self._loop,
            name="nfs-health-monitor",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "NfsHealthMonitor started (mount=%s, interval=%ds)",
            self._validator._mount_point,
            self._check_interval,
        )

    def stop(self) -> None:
        """Stop the background monitoring thread.

        Blocks until the thread exits (up to ``check_interval + 1`` seconds).
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._check_interval + 1)
            self._thread = None
        logger.info("NfsHealthMonitor stopped")

    # ------------------------------------------------------------------
    # Status accessors
    # ------------------------------------------------------------------

    @property
    def is_healthy(self) -> bool:
        """Current NFS mount health status."""
        with self._lock:
            return self._healthy

    def get_last_check(self) -> dict:
        """Return a copy of the last health check result dict."""
        with self._lock:
            return dict(self._last_check)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Main monitoring loop; runs in the background thread."""
        while not self._stop_event.wait(timeout=self._check_interval):
            self._run_check()

    def _run_check(self) -> None:
        """Execute one health check and update internal state."""
        try:
            result = self._validator.validate()
        except Exception as exc:  # noqa: BLE001
            result = {
                "healthy": False,
                "mount_point": str(self._validator._mount_point),
                "writable": False,
                "latency_ms": 0.0,
                "error": f"Unexpected exception during NFS health check: {exc}",
            }

        result["checked_at"] = time.time()

        with self._lock:
            self._healthy = bool(result.get("healthy", False))
            self._last_check = result

        if not self._healthy:
            logger.error(
                "NFS mount UNHEALTHY (mount=%s): %s",
                result.get("mount_point", "unknown"),
                result.get("error", "unknown error"),
            )
        else:
            logger.debug(
                "NFS mount healthy (mount=%s, latency=%.1fms)",
                result.get("mount_point", "unknown"),
                result.get("latency_ms", 0.0),
            )
