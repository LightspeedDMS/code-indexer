"""Temporal query snapshot persistence -- Story #1400 Phase 7.

FINAL LOCKED DESIGN (Codex's read-back-with-write-id verification adopted
over Opus's has_key-only check): every snapshot write is
`payload_cache.store_with_key(key, json.dumps(envelope))` -- the bare
`store()` generates a new UUID and cannot upsert a caller-chosen key.
After EVERY write (intermediate and final), the write is read back and its
`write_id` compared against what was just written. This is strictly more
correct than a bare `has_key()` check: the PostgreSQL PayloadCache backend
catches and suppresses store() failures, so if a PRIOR write already
succeeded under the same key, `has_key()` returns True even when the
NEWEST write silently no-op'd -- it cannot distinguish "the final write
succeeded" from "an old checkpoint is still there and the final write
silently failed". Comparing `write_id` catches exactly that case.

Final-write verification failure is JOB-FATAL
(TemporalSnapshotPersistenceError) -- a worker must never report success
without a durably-verified final snapshot.
"""

import json
import uuid
from typing import Any, Dict, Optional

from code_indexer.server.cache.payload_cache import CacheNotFoundError, PayloadCache

SNAPSHOT_VERSION = 1

# Messi #14 anti-unbounded-loop: a hard cap on reassembly pages. A temporal
# snapshot (a bounded list of QueryResult dicts up to fusion_fetch_limit) can
# never legitimately span this many pages; exceeding it indicates a corrupt
# or runaway CacheRetrievalResult.has_more chain, not a real snapshot.
_MAX_REASSEMBLY_PAGES = 1000


class TemporalSnapshotPersistenceError(Exception):
    """Raised when a temporal snapshot write cannot be verified by
    read-back (write_id mismatch or read failure). Job-fatal: the caller
    must mark the job failed with error_code
    TEMPORAL_SNAPSHOT_PERSISTENCE_FAILED, never report completed without a
    durably-verified final snapshot."""


class TemporalSnapshotReassemblyError(Exception):
    """Raised when multi-page reassembly exceeds _MAX_REASSEMBLY_PAGES, or a
    later page vanishes mid-chain after an earlier page indicated
    has_more=True -- indicates a corrupt/runaway read, not a legitimate
    partial snapshot."""


def temporal_snapshot_key(job_id: str) -> str:
    """The PayloadCache key for a temporal job's snapshot."""
    return f"temporal_query:{job_id}"


def read_temporal_snapshot(
    payload_cache: PayloadCache, job_id: str
) -> Optional[Dict[str, Any]]:
    """Read back and reassemble a temporal snapshot across all pages.

    Returns:
        The parsed snapshot envelope dict, or None if the key does not
        exist at all (genuine TTL expiry, or never written).

    Raises:
        TemporalSnapshotReassemblyError: a later page vanished mid-chain,
            or page count exceeded _MAX_REASSEMBLY_PAGES (Messi #14
            anti-unbounded-loop) -- both indicate corruption, never
            silently parsed as partial data.
    """
    key = temporal_snapshot_key(job_id)
    pages = []
    page_num = 0
    while page_num < _MAX_REASSEMBLY_PAGES:
        try:
            result = payload_cache.retrieve(key, page=page_num)
        except CacheNotFoundError:
            if page_num == 0:
                return None
            # A later page vanishing mid-chain is storage corruption, not a
            # legitimate "done" signal -- never silently parse partial data.
            raise TemporalSnapshotReassemblyError(
                f"Temporal snapshot for job {job_id!r} is missing page "
                f"{page_num} after a prior page indicated has_more=True."
            )
        pages.append(result.content)
        if not result.has_more:
            break
        page_num += 1
    else:
        raise TemporalSnapshotReassemblyError(
            f"Temporal snapshot for job {job_id!r} exceeded "
            f"{_MAX_REASSEMBLY_PAGES} reassembly pages -- treating as corrupt."
        )

    full_content = "".join(pages)
    parsed: Dict[str, Any] = json.loads(full_content)
    return parsed


def store_temporal_snapshot(
    payload_cache: PayloadCache,
    job_id: str,
    snapshot: Dict[str, Any],
    terminal: bool,
) -> str:
    """Write a temporal snapshot envelope and verify it by read-back.

    Args:
        payload_cache: the PayloadCache instance to write through.
        job_id: the temporal job id (used to derive the storage key).
        snapshot: the snapshot body (results/shards_completed/shards_total/ctx).
        terminal: whether this is the FINAL write for the job.

    Returns:
        The write_id that was written and verified.

    Raises:
        TemporalSnapshotPersistenceError: the read-back write_id did not
            match what was just written, or the read-back itself failed
            for any reason (missing key, corrupt reassembly, bad JSON).
    """
    key = temporal_snapshot_key(job_id)
    write_id = str(uuid.uuid4())
    envelope: Dict[str, Any] = dict(snapshot)
    envelope["write_id"] = write_id
    envelope["snapshot_version"] = SNAPSHOT_VERSION
    envelope["terminal"] = terminal

    payload_cache.store_with_key(key, json.dumps(envelope))

    try:
        read_back = read_temporal_snapshot(payload_cache, job_id)
    except Exception as exc:
        raise TemporalSnapshotPersistenceError(
            f"Temporal snapshot write verification failed for job {job_id!r}: "
            f"read-back raised {exc!r}"
        ) from exc

    if read_back is None or read_back.get("write_id") != write_id:
        raise TemporalSnapshotPersistenceError(
            f"Temporal snapshot write verification failed for job {job_id!r}: "
            f"expected write_id={write_id!r}, "
            f"got={(read_back or {}).get('write_id')!r}"
        )
    return write_id
