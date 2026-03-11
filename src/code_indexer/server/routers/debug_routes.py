"""Debug memory snapshot endpoints for CIDX Server.

Story #405: Debug Memory Endpoint

Provides localhost-only endpoints for diagnosing memory leaks and growth
patterns without restarting the server or attaching external profiling tools.

Security: Both endpoints require the request to originate from localhost
(127.0.0.1 or ::1). No authentication token is required; network restriction
is the sole security mechanism.
"""

import gc
import sys
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

debug_router = APIRouter(tags=["debug"])

# Module-level baseline storage (single slot - stores last snapshot taken).
_last_snapshot: Optional[Dict] = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _qualify_type_name(obj: object) -> str:
    """Return a fully-qualified type name for obj.

    Builtin types (module == "builtins" or None or "") return only the
    qualname (e.g. "dict", "list").  All other types include the module
    path (e.g. "datetime.datetime", "pathlib.PosixPath").
    """
    typ = type(obj)
    module = typ.__module__
    if not module or module == "builtins":
        return typ.__qualname__
    return f"{module}.{typ.__qualname__}"


def _check_localhost(request: Request) -> bool:
    """Return True iff the request originates from localhost.

    Accepts only 127.0.0.1 (IPv4) and ::1 (IPv6).  Returns False when
    request.client is None or the host is any other value.
    """
    if request.client is None:
        return False
    return request.client.host in ("127.0.0.1", "::1")


# ---------------------------------------------------------------------------
# Core snapshot logic
# ---------------------------------------------------------------------------


def get_snapshot() -> Dict:
    """Collect a memory snapshot via gc.get_objects().

    Performs gc.collect() first to minimise floating garbage, then walks
    every live object tracked by the garbage collector.  Returns a dict
    with all required AC1/AC3/AC6 fields and stores the result in the
    module-level _last_snapshot slot.
    """
    global _last_snapshot

    gc.collect()
    objects = gc.get_objects()
    overhead = sys.getsizeof(objects)

    count_by_type: Dict[str, int] = defaultdict(int)
    size_by_type: Dict[str, int] = defaultdict(int)

    for obj in objects:
        type_name = _qualify_type_name(obj)
        count_by_type[type_name] += 1
        try:
            size_by_type[type_name] += sys.getsizeof(obj)
        except (TypeError, ValueError, ReferenceError):
            pass

    total_objects = len(objects)
    total_size = sum(size_by_type.values())
    del objects  # release promptly

    by_count = dict(
        sorted(count_by_type.items(), key=lambda kv: kv[1], reverse=True)[:100]
    )
    by_size = dict(
        sorted(size_by_type.items(), key=lambda kv: kv[1], reverse=True)[:100]
    )

    snapshot: Dict = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "total_objects": total_objects,
        "total_size_bytes": total_size,
        "by_count": by_count,
        "by_size_bytes": by_size,
        "snapshot_overhead_bytes": overhead,
    }

    _last_snapshot = snapshot
    return snapshot


def compare_snapshot(baseline_timestamp: str) -> Optional[Dict]:
    """Compare current memory state against the stored baseline snapshot.

    Returns None if no stored snapshot exists or the stored snapshot's
    timestamp does not match baseline_timestamp (caller maps None to 404).

    Takes a fresh snapshot as the "current" reading, updates _last_snapshot,
    and returns the diff dict.
    """
    global _last_snapshot

    if _last_snapshot is None or _last_snapshot["timestamp"] != baseline_timestamp:
        return None

    baseline = _last_snapshot
    current = get_snapshot()  # also updates _last_snapshot

    # Build per-type diffs (union of keys from both snapshots)
    all_count_keys = set(baseline["by_count"]) | set(current["by_count"])
    by_count_diff = {
        k: current["by_count"].get(k, 0) - baseline["by_count"].get(k, 0)
        for k in all_count_keys
        if current["by_count"].get(k, 0) != baseline["by_count"].get(k, 0)
    }

    all_size_keys = set(baseline["by_size_bytes"]) | set(current["by_size_bytes"])
    by_size_diff = {
        k: current["by_size_bytes"].get(k, 0) - baseline["by_size_bytes"].get(k, 0)
        for k in all_size_keys
        if current["by_size_bytes"].get(k, 0) != baseline["by_size_bytes"].get(k, 0)
    }

    return {
        "baseline_timestamp": baseline_timestamp,
        "current_timestamp": current["timestamp"],
        "delta_objects": current["total_objects"] - baseline["total_objects"],
        "delta_size_bytes": current["total_size_bytes"] - baseline["total_size_bytes"],
        "by_count_diff": by_count_diff,
        "by_size_diff": by_size_diff,
    }


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@debug_router.get("/debug/memory-snapshot")
def memory_snapshot(request: Request) -> JSONResponse:
    """Return a memory snapshot of all live gc-tracked objects.

    Localhost-only (127.0.0.1 / ::1).  Returns 403 for all other origins.
    Each call replaces the stored baseline; only the most recent snapshot
    timestamp is valid as a baseline for /debug/memory-compare.
    """
    if not _check_localhost(request):
        return JSONResponse(
            status_code=403, content={"detail": "Forbidden: localhost only"}
        )

    snapshot = get_snapshot()
    return JSONResponse(content=snapshot)


@debug_router.get("/debug/memory-compare")
def memory_compare(request: Request, baseline: str) -> JSONResponse:
    """Compare current memory against the last stored snapshot baseline.

    Localhost-only.  Returns 403 for non-localhost, 404 when the requested
    baseline timestamp does not match the stored snapshot.
    """
    if not _check_localhost(request):
        return JSONResponse(
            status_code=403, content={"detail": "Forbidden: localhost only"}
        )

    result = compare_snapshot(baseline)
    if result is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"No baseline found for timestamp: {baseline}"},
        )
    return JSONResponse(content=result)
