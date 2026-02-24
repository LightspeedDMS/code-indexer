"""
SystemMetricsCollector - Singleton for system metrics collection (Story #696).

This module provides a thread-safe singleton that collects machine metrics
(CPU, memory, disk, network) using psutil. Values are cached with configurable
TTL to avoid excessive system calls.

Used by:
- Health endpoint for system status
- OTEL observable gauges for telemetry export
"""

from __future__ import annotations

import logging
import threading
import time
from threading import Lock
from typing import Any, Dict, Optional

import psutil

logger = logging.getLogger(__name__)

# Singleton instance and lock
_metrics_collector: Optional["SystemMetricsCollector"] = None
_collector_lock = Lock()

# Default cache TTL in seconds
DEFAULT_CACHE_TTL_SECONDS = 5.0


class SystemMetricsCollector:
    """
    Collects system metrics with caching support.

    This singleton class provides:
    - CPU usage percentage
    - Memory usage (percent and bytes)
    - Disk metrics (free space and I/O counters)
    - Network metrics (receive and transmit bytes)

    Values are cached with configurable TTL to minimize system calls.
    Thread-safe singleton pattern via get_system_metrics_collector().

    Psutil calls run in a background refresh thread (not under lock contention).
    The get_* methods only read cached values under the lock (fast dict reads),
    eliminating the possibility of psutil calls blocking concurrent request threads.
    """

    def __init__(self, cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS) -> None:
        """
        Initialize SystemMetricsCollector.

        Starts a background daemon thread that refreshes the metrics cache
        every cache_ttl_seconds. The get_* methods serve cached values without
        calling psutil directly under lock contention.

        Args:
            cache_ttl_seconds: Time-to-live for cached values in seconds;
                               also the background refresh interval.
        """
        self._cache_ttl = cache_ttl_seconds
        self._cached_metrics: Optional[Dict[str, Any]] = None
        self._last_cache_time: float = 0.0
        self._cache_lock = Lock()
        self._stop_event = threading.Event()

        # Start background refresh thread (daemon: won't block process shutdown)
        self._refresh_thread = threading.Thread(
            target=self._background_refresh_loop,
            daemon=True,
            name="SystemMetricsRefresh",
        )
        self._refresh_thread.start()

    def _background_refresh_loop(self) -> None:
        """
        Background thread that periodically refreshes the metrics cache.

        Runs until stop() is called or the process exits (daemon thread).
        The first refresh happens immediately on startup to pre-populate the cache.
        """
        while not self._stop_event.is_set():
            try:
                self._refresh_cache_background()
            except Exception as e:
                logger.warning(f"SystemMetricsCollector background refresh failed: {e}")
            # Sleep for TTL before next refresh; check stop_event periodically
            self._stop_event.wait(timeout=self._cache_ttl)

    def _refresh_cache_background(self) -> None:
        """Refresh all cached metrics from psutil (called from background thread)."""
        memory = psutil.virtual_memory()
        disk_usage = psutil.disk_usage("/")
        disk_io = psutil.disk_io_counters()
        net_io = psutil.net_io_counters()

        new_metrics = {
            "cpu_usage": psutil.cpu_percent(interval=None),
            "memory": {
                "percent": memory.percent,
                "used_bytes": memory.used,
            },
            "disk": {
                "free_bytes": disk_usage.free,
                "read_bytes": disk_io.read_bytes if disk_io else 0,
                "write_bytes": disk_io.write_bytes if disk_io else 0,
            },
            "network": {
                "receive_bytes": net_io.bytes_recv if net_io else 0,
                "transmit_bytes": net_io.bytes_sent if net_io else 0,
            },
        }

        # Update cached metrics under lock (fast dict swap)
        with self._cache_lock:
            self._cached_metrics = new_metrics
            self._last_cache_time = time.time()

    def _is_cache_valid(self) -> bool:
        """Check if cached metrics are still valid (must be called under _cache_lock)."""
        if self._cached_metrics is None:
            return False
        return (time.time() - self._last_cache_time) < self._cache_ttl

    def _refresh_cache(self) -> None:
        """Refresh all cached metrics from psutil (legacy, for direct callers)."""
        memory = psutil.virtual_memory()
        disk_usage = psutil.disk_usage("/")
        disk_io = psutil.disk_io_counters()
        net_io = psutil.net_io_counters()

        self._cached_metrics = {
            "cpu_usage": psutil.cpu_percent(interval=None),
            "memory": {
                "percent": memory.percent,
                "used_bytes": memory.used,
            },
            "disk": {
                "free_bytes": disk_usage.free,
                "read_bytes": disk_io.read_bytes if disk_io else 0,
                "write_bytes": disk_io.write_bytes if disk_io else 0,
            },
            "network": {
                "receive_bytes": net_io.bytes_recv if net_io else 0,
                "transmit_bytes": net_io.bytes_sent if net_io else 0,
            },
        }
        self._last_cache_time = time.time()

    def stop(self) -> None:
        """
        Stop the background refresh thread.

        Signals the background thread to exit and waits briefly for it to finish.
        Called by reset_system_metrics_collector() during testing and shutdown.
        """
        self._stop_event.set()
        self._refresh_thread.join(timeout=2.0)

    def _get_cached_metrics_copy(self) -> Optional[Dict[str, Any]]:
        """Return a copy of cached metrics under lock, or None if cache is empty."""
        with self._cache_lock:
            if self._cached_metrics is None:
                return None
            return dict(self._cached_metrics)

    def get_cpu_usage(self) -> float:
        """
        Get CPU usage percentage.

        Returns cached value populated by the background refresh thread.

        Returns:
            CPU usage as percentage (0-100)
        """
        with self._cache_lock:
            if self._cached_metrics is None:
                # Cache not yet populated - fall back to direct call for first request
                self._refresh_cache()
            assert self._cached_metrics is not None
            return float(self._cached_metrics["cpu_usage"])

    def get_memory_usage(self) -> Dict[str, Any]:
        """
        Get memory usage metrics.

        Returns cached value populated by the background refresh thread.

        Returns:
            Dict with 'percent' (0-100) and 'used_bytes'
        """
        with self._cache_lock:
            if self._cached_metrics is None:
                self._refresh_cache()
            assert self._cached_metrics is not None
            return dict(self._cached_metrics["memory"])

    def get_disk_metrics(self) -> Dict[str, Any]:
        """
        Get disk metrics.

        Returns cached value populated by the background refresh thread.

        Returns:
            Dict with 'free_bytes', 'read_bytes', 'write_bytes'
        """
        with self._cache_lock:
            if self._cached_metrics is None:
                self._refresh_cache()
            assert self._cached_metrics is not None
            return dict(self._cached_metrics["disk"])

    def get_network_metrics(self) -> Dict[str, Any]:
        """
        Get network I/O metrics.

        Returns cached value populated by the background refresh thread.

        Returns:
            Dict with 'receive_bytes', 'transmit_bytes'
        """
        with self._cache_lock:
            if self._cached_metrics is None:
                self._refresh_cache()
            assert self._cached_metrics is not None
            return dict(self._cached_metrics["network"])

    def get_all_metrics(self) -> Dict[str, Any]:
        """
        Get all system metrics at once.

        Returns cached values populated by the background refresh thread.
        The lock only protects a fast dict read - no psutil calls happen here.

        Returns:
            Dict with 'cpu_usage', 'memory', 'disk', 'network'
        """
        with self._cache_lock:
            if self._cached_metrics is None:
                self._refresh_cache()
            assert self._cached_metrics is not None
            return {
                "cpu_usage": float(self._cached_metrics["cpu_usage"]),
                "memory": dict(self._cached_metrics["memory"]),
                "disk": dict(self._cached_metrics["disk"]),
                "network": dict(self._cached_metrics["network"]),
            }


def get_system_metrics_collector(
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> SystemMetricsCollector:
    """
    Get the SystemMetricsCollector singleton instance.

    Args:
        cache_ttl_seconds: Cache TTL for the collector (only used on first call)

    Returns:
        SystemMetricsCollector singleton instance

    Thread-safe implementation using double-checked locking.
    """
    global _metrics_collector

    if _metrics_collector is not None:
        return _metrics_collector

    with _collector_lock:
        if _metrics_collector is not None:
            return _metrics_collector

        _metrics_collector = SystemMetricsCollector(cache_ttl_seconds)
        return _metrics_collector


def reset_system_metrics_collector() -> None:
    """
    Reset the SystemMetricsCollector singleton.

    Stops the background refresh thread of the current instance (if any),
    then clears the singleton reference. Primarily for testing purposes.
    """
    global _metrics_collector

    with _collector_lock:
        if _metrics_collector is not None:
            try:
                _metrics_collector.stop()
            except Exception as e:
                # Log but don't propagate - reset must complete even if stop fails
                logger.debug("Error stopping metrics collector during reset: %s", e)
        _metrics_collector = None
