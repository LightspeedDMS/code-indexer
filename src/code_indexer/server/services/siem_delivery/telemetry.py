"""OTEL instruments for SIEM delivery (``cidx.siem.*``).

Every call goes through ``peek_telemetry_manager()`` (never
``get_telemetry_manager()``), so an early caller can never lock in a
disabled telemetry config.  Instruments are created once per process.
Observable-gauge callbacks read an in-memory snapshot provider only -- never
the database -- and yield ``Observation`` objects.

The instrument cache is per-process telemetry wiring, not request state.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

PREFIX = "cidx.siem."
GAUGES = (
    "pending",
    "quarantined",
    "oldest_pending_age_seconds",
    "backlog_bytes_estimate",
    "projected_hours_to_disk_full",
    "halted",
    "capture_active",
    "unrecoverable",
    "capture_after_boundary",
    "capture_after_boundary_late",
)

_lock = threading.Lock()
_counters: Dict[str, Any] = {}
_histograms: Dict[str, Any] = {}
_gauges_registered = False


def _meter() -> Optional[Any]:
    from code_indexer.server.telemetry.manager import peek_telemetry_manager

    manager = peek_telemetry_manager()
    if manager is None:
        return None
    return manager.get_meter("cidx.siem_delivery")


def add_counter(
    name: str, value: int, attributes: Optional[Mapping[str, str]] = None
) -> None:
    """Add to counter ``cidx.siem.<name>``; telemetry never raises."""
    try:
        meter = _meter()
        if meter is None:
            return
        with _lock:
            counter = _counters.get(name)
            if counter is None:
                counter = meter.create_counter(PREFIX + name, unit="1")
                _counters[name] = counter
        counter.add(value, dict(attributes or {}))
    except Exception as exc:  # noqa: BLE001 - telemetry is never fatal
        logger.debug("SIEM telemetry counter %s failed: %s", name, exc)


def record_seconds(name: str, seconds: float, phase: str) -> None:
    """Record histogram ``cidx.siem.<name>`` with attribute ``phase``."""
    try:
        meter = _meter()
        if meter is None:
            return
        with _lock:
            histogram = _histograms.get(name)
            if histogram is None:
                histogram = meter.create_histogram(PREFIX + name, unit="s")
                _histograms[name] = histogram
        histogram.record(seconds, {"phase": phase})
    except Exception as exc:  # noqa: BLE001
        logger.debug("SIEM telemetry histogram %s failed: %s", name, exc)


def register_gauges(snapshot: Callable[[], Mapping[str, float]]) -> bool:
    """Register the observable gauges once per process; True when registered."""
    global _gauges_registered
    try:
        meter = _meter()
        if meter is None:
            return False
        from opentelemetry.metrics import Observation

        with _lock:
            if _gauges_registered:
                return True
            for gauge in GAUGES:

                def _callback(options: Any, _name: str = gauge) -> Any:
                    try:
                        value = snapshot().get(_name)
                    except Exception:  # noqa: BLE001
                        return
                    if value is not None:
                        yield Observation(value=float(value))

                meter.create_observable_gauge(
                    PREFIX + gauge, callbacks=[_callback], unit="1"
                )
            _gauges_registered = True
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("SIEM telemetry gauge registration failed: %s", exc)
        return False
