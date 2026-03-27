"""
Node Metrics Writer Service (Story #492: Cluster-Aware Dashboard).

Periodically collects system metrics from this node using psutil and writes
snapshots to the NodeMetricsBackend. The dashboard reads these snapshots to
render the cluster health carousel without polling psutil directly in the
HTTP request path.

Lifecycle::

    service = NodeMetricsWriterService(backend=backend, node_id="my-node")
    service.start()   # starts background daemon thread
    ...
    service.stop()    # signals thread to exit, waits for clean shutdown
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_THREAD_JOIN_GRACE_SECONDS = 5
_DEFAULT_RETENTION_SECONDS = 3600
_DEFAULT_WRITE_INTERVAL = 5


def _get_local_ip() -> str:
    """Detect the local non-loopback IP address via UDP connect trick."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
        finally:
            sock.close()
    except Exception as exc:
        logger.debug("Could not detect local IP, falling back to 127.0.0.1: %s", exc)
        return "127.0.0.1"


def _collect_memory_metrics() -> Dict[str, Any]:
    """Collect memory, swap, and process RSS metrics."""
    import psutil

    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    try:
        proc = psutil.Process()
        process_rss_mb = proc.memory_info().rss / (1024 * 1024)
    except Exception as exc:
        logger.debug("Could not get process RSS: %s", exc)
        process_rss_mb = 0.0

    return {
        "memory_percent": mem.percent,
        "memory_used_bytes": mem.used,
        "process_rss_mb": process_rss_mb,
        "swap_used_mb": swap.used / (1024 * 1024),
        "swap_total_mb": swap.total / (1024 * 1024),
    }


def _collect_io_metrics_with_state(state: Dict[str, Any]) -> Dict[str, float]:
    """Collect disk I/O and network I/O as KB/s rates using delta calculation.

    On the first call (empty state), all rates are 0.0.  Subsequent calls
    compute rate = (current_bytes - previous_bytes) / elapsed_seconds / 1024.
    Negative deltas (counter reset / OS reboot) are clamped to 0.0.

    Args:
        state: A mutable dict persisted between calls.  Keys used internally:
               ``_prev_disk_read``, ``_prev_disk_write``, ``_prev_net_rx``,
               ``_prev_net_tx``, ``_prev_io_time``.

    Returns:
        Dict with keys: disk_read_kb_s, disk_write_kb_s, net_rx_kb_s, net_tx_kb_s.
        All values are floats (KB/s).
    """
    import psutil

    now = time.monotonic()

    # --- Disk I/O ---
    disk_read_bytes: float = 0.0
    disk_write_bytes: float = 0.0
    try:
        disk_io = psutil.disk_io_counters()
        if disk_io:
            disk_read_bytes = float(disk_io.read_bytes)
            disk_write_bytes = float(disk_io.write_bytes)
    except Exception as exc:
        logger.debug("Could not get disk I/O counters: %s", exc)

    # --- Network I/O ---
    net_rx_bytes: float = 0.0
    net_tx_bytes: float = 0.0
    try:
        net_io = psutil.net_io_counters()
        if net_io:
            net_rx_bytes = float(net_io.bytes_recv)
            net_tx_bytes = float(net_io.bytes_sent)
    except Exception as exc:
        logger.debug("Could not get network I/O counters: %s", exc)

    prev_time: Optional[float] = state.get("_prev_io_time")

    if prev_time is None:
        # First call: store baseline, return all zeros
        state["_prev_disk_read"] = disk_read_bytes
        state["_prev_disk_write"] = disk_write_bytes
        state["_prev_net_rx"] = net_rx_bytes
        state["_prev_net_tx"] = net_tx_bytes
        state["_prev_io_time"] = now
        return {
            "disk_read_kb_s": 0.0,
            "disk_write_kb_s": 0.0,
            "net_rx_kb_s": 0.0,
            "net_tx_kb_s": 0.0,
        }

    elapsed = now - prev_time

    def _rate_kb_s(current: float, previous: float) -> float:
        """Compute KB/s rate; clamp negative delta (counter reset) to 0."""
        delta = current - previous
        if elapsed <= 0.0 or delta < 0.0:
            return 0.0
        return delta / elapsed / 1024.0

    result = {
        "disk_read_kb_s": _rate_kb_s(disk_read_bytes, state["_prev_disk_read"]),
        "disk_write_kb_s": _rate_kb_s(disk_write_bytes, state["_prev_disk_write"]),
        "net_rx_kb_s": _rate_kb_s(net_rx_bytes, state["_prev_net_rx"]),
        "net_tx_kb_s": _rate_kb_s(net_tx_bytes, state["_prev_net_tx"]),
    }

    # Update state for next call
    state["_prev_disk_read"] = disk_read_bytes
    state["_prev_disk_write"] = disk_write_bytes
    state["_prev_net_rx"] = net_rx_bytes
    state["_prev_net_tx"] = net_tx_bytes
    state["_prev_io_time"] = now

    return result


def _collect_volume_info() -> List[Dict[str, Any]]:
    """Collect disk partition usage for each mounted volume."""
    import psutil

    volumes: List[Dict[str, Any]] = []
    try:
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                volumes.append(
                    {
                        "mount_point": part.mountpoint,
                        "device": part.device,
                        "fstype": part.fstype,
                        "total_gb": round(usage.total / (1024**3), 1),
                        "used_gb": round(usage.used / (1024**3), 1),
                        "free_gb": round(usage.free / (1024**3), 1),
                        "used_percent": usage.percent,
                    }
                )
            except Exception as exc:
                logger.debug(
                    "Could not get disk usage for %s: %s", part.mountpoint, exc
                )
    except Exception as exc:
        logger.debug("Could not enumerate disk partitions: %s", exc)

    return volumes


def _collect_metrics(node_id: str, node_ip: str, io_state: Dict[str, Any]) -> dict:
    """Collect all system metrics for this node using psutil.

    Args:
        node_id: Identifier string for this node.
        node_ip: IP address string for this node.
        io_state: Mutable dict persisted between calls for I/O rate calculation.
                  Pass the same dict on every call for the same service instance.
    """
    import psutil

    try:
        from code_indexer import __version__ as server_version
    except Exception as exc:
        logger.debug("Could not import server version: %s", exc)
        server_version = ""

    memory = _collect_memory_metrics()
    io = _collect_io_metrics_with_state(io_state)
    volumes = _collect_volume_info()

    return {
        "node_id": node_id,
        "node_ip": node_ip,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cpu_usage": psutil.cpu_percent(interval=None),
        "memory_percent": memory["memory_percent"],
        "memory_used_bytes": memory["memory_used_bytes"],
        "process_rss_mb": memory["process_rss_mb"],
        "index_memory_mb": 0.0,
        "swap_used_mb": memory["swap_used_mb"],
        "swap_total_mb": memory["swap_total_mb"],
        "disk_read_kb_s": io["disk_read_kb_s"],
        "disk_write_kb_s": io["disk_write_kb_s"],
        "net_rx_kb_s": io["net_rx_kb_s"],
        "net_tx_kb_s": io["net_tx_kb_s"],
        "volumes_json": json.dumps(volumes),
        "server_version": server_version,
    }


class NodeMetricsWriterService:
    """Background service that collects and persists node metrics snapshots."""

    def __init__(
        self,
        backend: Any,
        node_id: Optional[str] = None,
        write_interval: int = _DEFAULT_WRITE_INTERVAL,
        retention_seconds: int = _DEFAULT_RETENTION_SECONDS,
    ) -> None:
        self._backend = backend
        self._node_id: str = node_id if node_id is not None else socket.gethostname()
        self._node_ip: str = _get_local_ip()
        self._write_interval = write_interval
        self._retention_seconds = retention_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Persistent state for I/O rate calculation across write cycles
        self._io_state: Dict[str, Any] = {}
        self._io_state_lock = threading.Lock()  # Bug #548: protect concurrent access

    @property
    def node_id(self) -> str:
        """The node identifier used in snapshots."""
        return self._node_id

    @property
    def node_ip(self) -> str:
        """The detected local IP address for this node."""
        return self._node_ip

    def write_once(self) -> None:
        """Collect metrics, write one snapshot, and clean up old records."""
        try:
            with self._io_state_lock:
                snapshot = _collect_metrics(
                    self._node_id, self._node_ip, self._io_state
                )
            self._backend.write_snapshot(snapshot)
        except Exception:
            logger.exception(
                "NodeMetricsWriterService [%s]: error writing snapshot", self._node_id
            )

        try:
            cutoff = datetime.now(timezone.utc) - timedelta(
                seconds=self._retention_seconds
            )
            deleted = self._backend.cleanup_older_than(cutoff)
            if deleted:
                logger.debug(
                    "NodeMetricsWriterService [%s]: cleaned up %d old snapshots",
                    self._node_id,
                    deleted,
                )
        except Exception:
            logger.exception(
                "NodeMetricsWriterService [%s]: error during cleanup", self._node_id
            )

    def start(self) -> None:
        """Start the background metrics writer thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning(
                "NodeMetricsWriterService [%s]: already running", self._node_id
            )
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name=f"NodeMetricsWriter-{self._node_id}",
        )
        self._thread.start()
        logger.info(
            "NodeMetricsWriterService [%s]: started (interval=%ds)",
            self._node_id,
            self._write_interval,
        )

    def stop(self) -> None:
        """Signal the background thread to stop and wait for it to exit."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=self._write_interval + _THREAD_JOIN_GRACE_SECONDS)
        logger.info("NodeMetricsWriterService [%s]: stopped", self._node_id)

    def _writer_loop(self) -> None:
        """Background thread main loop."""
        logger.debug(
            "NodeMetricsWriterService [%s]: writer loop started", self._node_id
        )
        while not self._stop_event.is_set():
            self.write_once()
            self._stop_event.wait(timeout=self._write_interval)

        logger.debug(
            "NodeMetricsWriterService [%s]: writer loop exiting", self._node_id
        )
