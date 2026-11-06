"""``cidx_metadata_outbox_depth``: rows waiting in the metadata outbox.

The rollback gate reads it: before a rollback proceeds the gauge is 0 on the
leader (and ``SELECT count(*) FROM metadata_outbox`` returns 0). A PULL-based
OTEL ObservableGauge; its callback is one COUNT on the registry per export
tick, the same on every node since the outbox is shared.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

OUTBOX_DEPTH_METRIC = "cidx_metadata_outbox_depth"


def depth_observations(depth_fn: Callable[[], int]) -> Iterator[Any]:
    """Yield one Observation of the current depth; none if it cannot be read
    (an export tick must never raise)."""
    from opentelemetry.metrics import Observation

    try:
        depth = int(depth_fn())
    except Exception as exc:
        logger.debug("metadata outbox depth unavailable for export: %s", exc)
        return
    yield Observation(depth)


def register_outbox_depth_gauge(meter: Any, depth_fn: Callable[[], int]) -> Any:
    """Register the gauge on *meter*; returns the instrument."""
    return meter.create_observable_gauge(
        name=OUTBOX_DEPTH_METRIC,
        description="Control-plane metadata outbox rows not yet replayed to "
        "the alias / write-mode files (rollback gate: 0)",
        unit="1",
        callbacks=[lambda _options: depth_observations(depth_fn)],
    )
