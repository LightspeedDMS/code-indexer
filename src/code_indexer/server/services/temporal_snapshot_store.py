"""Temporal query snapshot persistence -- Story #1400 Phase 7, Bug #1421 fix.

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

Bug #1421: the temporal worker (temporal_worker.py) writes GROW-THEN-SHRINK
checkpoints while a query is in flight -- intermediate cumulative writes
(possibly spanning multiple PayloadCache pages) followed by one FINAL
fusion-truncated write with FEWER pages. read_temporal_snapshot() reads
pages via separate, non-isolated payload_cache.retrieve(key, page=n) calls
in a loop; if a rewrite lands between two of those calls, a later page can
fall out of range (CacheNotFoundError) or, more subtly, the reassembled
content can be silently spliced from TWO different generations (old early
pages + new later pages) without ever raising -- a page-count-preserving or
still-in-range rewrite is otherwise invisible to a naive page loop. Both
symptoms are detected via _read_temporal_snapshot_attempt's total_pages
consistency check plus a final json.loads validation, and resolved by
retrying the WHOLE reassembly from page 0 against the latest write, bounded
by _MAX_REASSEMBLY_RETRY_ATTEMPTS (Messi #14 anti-unbounded-loop) so a
genuinely unrecoverable failure still surfaces as a hard, LOGGED error
rather than retrying forever.
"""

import json
import logging
import uuid
from typing import Any, Dict, Optional

from code_indexer.server.cache.payload_cache import CacheNotFoundError, PayloadCache

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1

# Messi #14 anti-unbounded-loop: a hard cap on reassembly pages. A temporal
# snapshot (a bounded list of QueryResult dicts up to fusion_fetch_limit) can
# never legitimately span this many pages; exceeding it indicates a corrupt
# or runaway CacheRetrievalResult.has_more chain, not a real snapshot.
_MAX_REASSEMBLY_PAGES = 1000

# Messi #14 anti-unbounded-loop: bounds how many times the WHOLE reassembly
# (all pages, from page 0) is ATTEMPTED in total -- the first pass plus any
# retries triggered by a detected concurrent worker checkpoint rewrite
# (Bug #1421). E.g. a value of 5 means at most 5 full reassembly passes,
# not "1 initial pass + 5 retries". The worker debounces checkpoint writes
# at CHECKPOINT_MIN_GAP_SECONDS=2.0s (temporal_worker.py), so a retry that
# restarts from page 0 overwhelmingly lands in a stable window well before
# this cap is reached; exhausting it indicates a persistent, non-converging
# rewrite pattern, not a single unlucky interleaving.
_MAX_REASSEMBLY_RETRY_ATTEMPTS = 5


class TemporalSnapshotPersistenceError(Exception):
    """Raised when a temporal snapshot write cannot be verified by
    read-back (write_id mismatch or read failure). Job-fatal: the caller
    must mark the job failed with error_code
    TEMPORAL_SNAPSHOT_PERSISTENCE_FAILED, never report completed without a
    durably-verified final snapshot."""


class TemporalSnapshotReassemblyError(Exception):
    """Raised when multi-page reassembly exceeds _MAX_REASSEMBLY_PAGES
    (genuine corruption/runaway chain), or when a concurrent worker
    checkpoint rewrite (Bug #1421) keeps invalidating the reassembly across
    _MAX_REASSEMBLY_RETRY_ATTEMPTS retries -- both indicate the caller
    cannot obtain a complete, correct snapshot, never a legitimate partial
    one silently passed off as complete."""


class _ConcurrentRewriteDetected(Exception):
    """Internal signal (never surfaced to callers of read_temporal_snapshot):
    a concurrent worker checkpoint write was detected mid-reassembly (Bug
    #1421's grow-then-shrink race) -- a later page vanished after an
    earlier page indicated has_more=True, total_pages changed between two
    page reads within the same attempt, or the joined content failed to
    parse as JSON (a page-count-preserving rewrite spliced across two
    generations). The caller must retry the WHOLE reassembly from page 0
    against the new write, bounded by _MAX_REASSEMBLY_RETRY_ATTEMPTS."""


def temporal_snapshot_key(job_id: str) -> str:
    """The PayloadCache key for a temporal job's snapshot."""
    return f"temporal_query:{job_id}"


def _read_temporal_snapshot_attempt(
    payload_cache: PayloadCache, job_id: str
) -> Optional[Dict[str, Any]]:
    """A single reassembly attempt (one full pass over all pages).

    Returns:
        The parsed snapshot envelope dict, or None if page 0 does not
        exist at all (genuine TTL expiry, or never written -- NOT a race,
        never retried).

    Raises:
        _ConcurrentRewriteDetected: a later page vanished mid-chain, the
            total_pages metadata changed between two page reads, or the
            joined content failed to parse as JSON -- all symptoms of Bug
            #1421's concurrent checkpoint rewrite race. Caller retries.
        TemporalSnapshotReassemblyError: page count exceeded
            _MAX_REASSEMBLY_PAGES (Messi #14 anti-unbounded-loop) --
            indicates genuine corruption/runaway chain, never retried.
    """
    key = temporal_snapshot_key(job_id)
    pages = []
    page_num = 0
    expected_total_pages: Optional[int] = None
    while page_num < _MAX_REASSEMBLY_PAGES:
        try:
            result = payload_cache.retrieve(key, page=page_num)
        except CacheNotFoundError:
            if page_num == 0:
                return None
            # A later page vanishing mid-chain after has_more=True is the
            # literal Bug #1421 symptom: the worker's smaller FINAL write
            # landed between this and a prior page read. Not corruption --
            # a detected rewrite, retried by the caller.
            raise _ConcurrentRewriteDetected(
                f"Temporal snapshot for job {job_id!r} is missing page "
                f"{page_num} after a prior page indicated has_more=True."
            )
        if expected_total_pages is None:
            expected_total_pages = result.total_pages
        elif result.total_pages != expected_total_pages:
            # The page count itself changed mid-read (a growing or
            # page-count-preserving-but-content-differing rewrite) --
            # continuing would silently splice bytes from two different
            # generations into one "snapshot". Detected, not silently
            # parsed.
            raise _ConcurrentRewriteDetected(
                f"Temporal snapshot for job {job_id!r} total_pages changed "
                f"mid-reassembly ({expected_total_pages} -> "
                f"{result.total_pages}) while reading page {page_num}."
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
    try:
        parsed: Dict[str, Any] = json.loads(full_content)
    except json.JSONDecodeError as exc:
        # A stable total_pages count does not by itself guarantee the
        # bytes came from ONE generation -- a same-page-count rewrite can
        # still splice content across generations. A failed parse is the
        # last-resort detector for that case.
        raise _ConcurrentRewriteDetected(
            f"Temporal snapshot for job {job_id!r} failed to parse after "
            f"reassembly (likely spliced across a concurrent checkpoint "
            f"rewrite): {exc}"
        ) from exc
    return parsed


def read_temporal_snapshot(
    payload_cache: PayloadCache, job_id: str
) -> Optional[Dict[str, Any]]:
    """Read back and reassemble a temporal snapshot across all pages.

    Bug #1421: the temporal worker checkpoints while a query is in flight,
    so a rewrite can legitimately land between two of this function's
    separate page-read calls. Rather than surfacing that as a raw error to
    the client, a detected rewrite triggers a transparent retry of the
    WHOLE reassembly from page 0 against the latest write, bounded by
    _MAX_REASSEMBLY_RETRY_ATTEMPTS (total attempts, including the first
    pass) so a genuinely non-converging rewrite pattern still fails loud
    (logged) instead of retrying forever.

    Returns:
        The parsed snapshot envelope dict, or None if the key does not
        exist at all (genuine TTL expiry, or never written).

    Raises:
        TemporalSnapshotReassemblyError: page count exceeded
            _MAX_REASSEMBLY_PAGES (genuine corruption, never retried), or
            _MAX_REASSEMBLY_RETRY_ATTEMPTS total attempts were all
            invalidated by a concurrent rewrite -- both indicate the
            caller cannot obtain a complete, correct snapshot, never a
            partial one silently passed off as complete. Logged at ERROR
            with the job_id before being raised (Bug #1421: this failure
            previously produced zero server-side log entries).
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_REASSEMBLY_RETRY_ATTEMPTS + 1):
        try:
            return _read_temporal_snapshot_attempt(payload_cache, job_id)
        except _ConcurrentRewriteDetected as exc:
            last_exc = exc
            if attempt < _MAX_REASSEMBLY_RETRY_ATTEMPTS:
                logger.warning(
                    "temporal snapshot job %s: concurrent checkpoint "
                    "rewrite detected during reassembly (attempt %d/%d) -- "
                    "retrying from page 0 against the latest write: %s",
                    job_id,
                    attempt,
                    _MAX_REASSEMBLY_RETRY_ATTEMPTS,
                    exc,
                )
                # else: this was the last permitted attempt -- fall through
                # to the ERROR log + raise below instead of claiming a
                # retry that will not happen.

    logger.error(
        "temporal snapshot job %s: reassembly failed after %d attempts -- "
        "concurrent checkpoint rewrites kept invalidating the read; "
        "surfacing as a hard error (last detected cause: %s)",
        job_id,
        _MAX_REASSEMBLY_RETRY_ATTEMPTS,
        last_exc,
    )
    raise TemporalSnapshotReassemblyError(
        f"Temporal snapshot for job {job_id!r} could not be reassembled "
        f"after {_MAX_REASSEMBLY_RETRY_ATTEMPTS} attempts due to repeated "
        f"concurrent checkpoint rewrites."
    ) from last_exc


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
