"""Health view of the search event log writer (Bug #2114).

The writer counts the events it drops on queue overflow (per process). This
module turns its O(1) snapshot into the /health `search_events` field and a
DEGRADED reason. Pure in-memory reads: safe on the health hot path.
"""

import logging
import time
from typing import Any, Dict, List, Optional

from code_indexer.server.models.api_models import SearchEventLogHealthInfo
from code_indexer.server.utils.runtime_node_id import resolve_runtime_node_id

logger = logging.getLogger(__name__)

# A drop keeps the node DEGRADED for this long after the most recent one.
# The writer drains every 5 s, so a drop means sustained overload, not a
# blip; 15 minutes spans many dashboard and monitoring polls, so an operator
# sees it, and it clears by itself once the load is gone (one operator, no
# manual reset). The count itself stays on the response until restart.
SEARCH_EVENT_DROP_WINDOW_SECONDS = 15 * 60


def search_event_health_info(writer: Any) -> Optional[SearchEventLogHealthInfo]:
    """This process's drop count, or None when no writer is running."""
    if writer is None:
        return None
    drops = writer.drop_snapshot()
    return SearchEventLogHealthInfo(
        events_dropped_since_start=drops.dropped,
        seconds_since_last_drop=drops.seconds_since_last_drop,
        node_id=resolve_runtime_node_id(),
    )


def process_search_event_health() -> Optional[SearchEventLogHealthInfo]:
    """The serving process's drop count (the server app's writer), or None
    when it has no running writer. Never raises: a read failure is logged
    and reports None, so no health surface can fail on it."""
    try:
        from ..app import app as _server_app

        # app comes from app.py's lazy module __getattr__, so mypy's type for it
        # depends on import-cycle check order; bind it as Any explicitly.
        server_app: Any = _server_app

        writer = getattr(server_app.state, "search_event_log_writer", None)
        return search_event_health_info(writer)
    except Exception as exc:
        logger.warning(
            "Search event log health collection failed (%s): %s",
            type(exc).__name__,
            exc,
        )
        return None


def search_event_drop_reasons(info: Optional[SearchEventLogHealthInfo]) -> List[str]:
    """A DEGRADED reason while the last drop is within the window."""
    if info is None or info.seconds_since_last_drop is None:
        return []
    if info.seconds_since_last_drop > SEARCH_EVENT_DROP_WINDOW_SECONDS:
        return []
    return [
        f"Search event log dropped {info.events_dropped_since_start} search "
        f"events in a server process on node {info.node_id} since that "
        f"process started (last "
        f"{int(info.seconds_since_last_drop)}s ago): its recording queue was "
        "full or a write to the store failed; Query Analytics exports are "
        "missing those searches"
    ]


def search_event_log_window(writer: Any) -> Dict[str, Any]:
    """Story #2117: what the Query Analytics Export page states about the
    data an export can contain.

    `retention_days` is the live setting (rows older are pruned), read from
    the COMMITTED runtime configuration -- a SQLite worker never reloads
    another worker's save, so the process's cached copy can be stale;
    `retention_unavailable` is True (and `retention_days` None) when that
    read failed or held no whole number of days (logged at WARNING).
    `oldest_timestamp` is the oldest stored row as "YYYY-MM-DD HH:MM:SS UTC"
    (one indexed MIN read; None when the log is empty or not wired);
    `oldest_unavailable` is True when that read failed (logged at WARNING).
    """
    from code_indexer.server.services.config_service import get_config_service

    window: Dict[str, Any] = {
        "retention_days": None,
        "retention_unavailable": False,
        "oldest_timestamp": None,
        "oldest_unavailable": False,
    }
    try:
        _version, retention = get_config_service().read_committed_setting(
            "search_event_log_retention_days"
        )
        if isinstance(retention, bool) or not isinstance(retention, int):
            raise TypeError("retention is not a whole number of days")
        window["retention_days"] = retention
    except Exception as exc:  # noqa: BLE001 -- the page still renders
        logger.warning(
            "search event log: committed retention setting not read: %s",
            type(exc).__name__,
        )
        window["retention_unavailable"] = True
    if writer is None:
        return window
    try:
        oldest = writer.backend.oldest_timestamp()
    except Exception as exc:  # noqa: BLE001 -- the page still renders
        logger.warning(
            "search event log: oldest timestamp not read: %s", type(exc).__name__
        )
        window["oldest_unavailable"] = True
        return window
    if oldest is not None:
        window["oldest_timestamp"] = time.strftime(
            "%Y-%m-%d %H:%M:%S UTC", time.gmtime(oldest)
        )
    return window
