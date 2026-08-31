"""Fleet-migration per-repo failure quarantine (Issue #1477).

`FleetMigrationScheduler._run_next_candidate()` always picks the FIRST
not-yet-migrated golden repo (alias-sorted order, `discovery.py`'s
`enumerate_fleet_migration_candidates()`), with no memory of prior
attempts. `is_repo_already_migrated()` is a pure disk-state check with no
awareness of previous failures either. If a repo's migration throws every
single time -- e.g. genuinely corrupt legacy `vector_*.json` data that
`storage/shared/collection_migration.py`'s `consolidate_collection_in_place`
(via `IDIndexManager.scan_vectors_for_id_map`) correctly refuses to
auto-resolve (Messi Rule #13 Anti-Silent-Failure: it never silently picks a
winner among duplicate point_ids) -- that repo is retried on EVERY tick
forever, permanently starving every alphabetically-later repo in the fleet
(a real, live-observed incident: Issue #1477).

This module implements the quarantine mechanism, deliberately reusing this
project's own established pattern rather than inventing a new one:

- Threshold-based circuit breaker, mirroring
  `description_refresh_scheduler.py`'s `PROMPT_FAILURE_QUARANTINE_THRESHOLD`
  (3 consecutive failures quarantine a repo).
- Persisted, cross-restart state via the SAME golden-repo-metadata storage
  backend `GoldenRepoManager` already injects (`_sqlite_backend` -- SQLite
  in solo mode, PostgreSQL in cluster mode), the identical injection
  convention `golden_repo_reconciler.py` established for its own
  `golden_repo_reconcile_breaker_state` table (Messi Rule #4: anti-
  duplication -- no new storage mechanism here either).
- Auto-clear ONLY on genuine evidence of on-disk state change since the
  quarantine's last recorded failure -- mirroring
  `description_refresh_scheduler.py`'s commit-based auto-clear gate (Bug
  #1096's lesson: a bare retry must never look like evidence of change,
  or quarantine is defeated for exactly the worst case it exists to
  handle). The equivalent signal for fleet migration (no single "commit"
  concept) is a cheap, BOUNDED 4-level-recursive fingerprint (see
  `_SIGNATURE_MAX_SHARD_DEPTH`/`_collect_dir_state_tokens` -- never an
  unbounded walk, never O(files in the whole collection)) of each
  directory's own mtime/entry-count PLUS each leaf file's own
  mtime/size/ctime, across the candidate's semantic-collection and
  temporal-shard directories.

Deliberately does NOT add a job/subprocess timeout (this project's
indexing/migration path has none, by design -- see CLAUDE.md's "Indexing
Path Has No Job/Subprocess/Per-File Timeouts"). This is purely about
SKIPPING a repo after N failures so the scheduler advances, never about
time-bounding a single attempt.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from code_indexer.server.services.fleet_migration.alias_normalization import (
    normalize_golden_alias,
)
from code_indexer.server.services.fleet_migration.discovery import (
    FleetMigrationCandidate,
    enumerate_fleet_migration_candidates,
)
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout
from code_indexer.storage.sqlite_chunk_store import chunk_store_has_real_data

logger = logging.getLogger(__name__)

#: Mirrors description_refresh_scheduler.py's
#: PROMPT_FAILURE_QUARANTINE_THRESHOLD -- N consecutive failures for the
#: same golden_alias quarantine it until a genuine on-disk state change is
#: observed (or a success occurs).
FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD = 3


class QuarantineStateUnavailableError(Exception):
    """Raised when a backend that IS configured (not the deliberate "no
    backend at all -- tracking disabled" case) genuinely FAILS to
    service a quarantine-state operation -- either a READ
    (`get_failure_state()`/`is_quarantined()`, Finding A, Codex round-3
    review, live-reproduced) or a WRITE (`record_migration_failure()`,
    Finding D, Codex round-4 review, live-reproduced). Both directions
    are the SAME underlying problem -- "we cannot currently trust the
    persisted quarantine state" -- reusing one exception type rather than
    inventing a second (Messi Rule #4: anti-duplication).

    This must NEVER be silently swallowed: on the READ side, swallowing
    it into a bare `False`/`None` return previously meant "assume not
    quarantined, retry the same broken candidate" -- with a PERSISTENT
    backend outage, that recreates the EXACT fleet-starvation bug #1477
    reports (the scheduler proceeds with the SAME first candidate on
    every single tick forever). On the WRITE side, swallowing it meant
    the consecutive-failure count is NEVER incremented, so
    `is_quarantined()` keeps reading "0 failures, not quarantined"
    forever -- the identical starvation bug via a different mechanism.
    Callers (the scheduler) must catch this specifically and ABORT the
    scheduling tick -- never treat it as "not quarantined" (retries the
    same repo forever) nor as "nothing_to_migrate" (misleading -- the
    truth is "we genuinely don't know").
    """


def _get_quarantine_backend(golden_repo_manager: Any) -> Optional[Any]:
    """Resolve the shared golden-repo-metadata storage backend used to
    persist fleet-migration failure/quarantine state (Issue #1477).

    This is the SAME backend instance `GoldenRepoManager` already uses for
    its own registry rows (`_sqlite_backend` -- SQLite in solo mode,
    PostgreSQL in cluster mode via StorageFactory), reused here exactly
    like `golden_repo_reconciler.py`'s own `_get_breaker_backend()` helper
    (Messi Rule #4: anti-duplication -- no new storage layer). Returns
    None if unavailable; callers degrade gracefully to "quarantine
    tracking disabled" rather than raising -- a missing backend must never
    block migration itself.
    """
    return getattr(golden_repo_manager, "_sqlite_backend", None)


def _stat_dir_or_none(path: Path) -> Optional[os.stat_result]:
    """Stat `path`, distinguishing a normal "ABSENT" outcome (the path
    genuinely does not exist -- not logged, an expected non-error state,
    e.g. a collection not yet created) from a genuine "UNSCANNABLE"
    problem (path exists but cannot be stat'd -- e.g. permission denied,
    loudly logged at WARNING per Messi Rule #13 Anti-Silent-Failure).

    Returns None in EITHER case -- callers distinguish "ABSENT" from
    "UNSCANNABLE" purely via the log side effect, since both must render
    the SAME distinct absent-like token to the caller (a directory that
    cannot be examined is, from the signature's point of view, no more
    trustworthy than one that does not exist).
    """
    try:
        return path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(
            "Issue #1477: quarantine signature could not stat %s (%s) -- "
            "treating as unreadable, distinct from a genuinely absent "
            "path.",
            path,
            exc,
        )
        return None


def _scan_dir_entries(path: Path) -> Optional[Any]:
    """Single-`os.scandir()`-pass collection of `path`'s immediate
    entries (Finding C item 1, Codex round-3 review): each `DirEntry` is
    `stat()`'d EXACTLY ONCE (never an `is_dir()` check followed by a
    SEPARATE `stat()` call) -- `stat.S_ISDIR` on that one result
    determines directory-vs-file.

    Returns `(file_tokens, subdirs)`, or None if the scan itself failed
    entirely (loudly logged). A single unreadable ENTRY (e.g. a race with
    a concurrent delete) does not fail the whole scan -- it is recorded
    as its own "UNREADABLE" token (also loudly logged) and scanning
    continues, since one bad entry must not invalidate the rest of the
    directory's fingerprint.
    """
    file_tokens: List[str] = []
    subdirs: List[Path] = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError as entry_exc:
                    logger.warning(
                        "Issue #1477: quarantine signature could not stat "
                        "entry %s inside %s (%s) -- recording as "
                        "UNREADABLE for this entry only.",
                        entry.name,
                        path,
                        entry_exc,
                    )
                    file_tokens.append(f"{entry.name}:UNREADABLE")
                    continue
                if stat.S_ISDIR(entry_stat.st_mode):
                    subdirs.append(Path(entry.path))
                else:
                    # Finding E (Codex round-4 review, residual): mtime_ns
                    # + size alone (Finding B's original fix) misses a
                    # rewrite that preserves BOTH exact values (e.g. a
                    # tool that explicitly restores timestamps after
                    # writing, or a coarse-timestamp filesystem).
                    # st_ctime_ns (inode change time) is free -- same
                    # stat() call already made, no new syscalls -- and
                    # changes on essentially any real content/metadata
                    # modification, including ones that deliberately
                    # preserve mtime.
                    #
                    # Known, accepted, self-healing residual (Codex
                    # round-5 review): a bare metadata-only change (e.g.
                    # `chmod`, no content change at all) ALSO bumps
                    # ctime and can falsely auto-clear a quarantine.
                    # This is a bounded, secondary risk, not a
                    # starvation vector -- the repo simply re-quarantines
                    # on the very next genuine failed attempt, at the
                    # cost of one extra unnecessary migration attempt,
                    # which is far cheaper than permanent starvation. Do
                    # not chase this further with more code complexity.
                    file_tokens.append(
                        f"{entry.name}:{entry_stat.st_mtime_ns}:"
                        f"{entry_stat.st_size}:{entry_stat.st_ctime_ns}"
                    )
    except OSError as exc:
        logger.warning(
            "Issue #1477: quarantine signature could not scan directory "
            "entries for %s (%s) -- treating as unscannable.",
            path,
            exc,
        )
        return None
    return file_tokens, subdirs


def _dir_state_token(path: Path) -> Any:
    """A fingerprint of one directory's own on-disk state (Finding B +
    Finding C item 1, Codex round-3 review): the directory's own mtime
    (nanosecond resolution) plus entry count (detects add/remove/rename
    of directory entries), PLUS each FILE entry's own
    `(name, mtime_ns, size)` (detects an operator repairing corrupt data
    by rewriting an existing file's CONTENT in place, same filename, no
    add/remove -- round-1/2's directory-only signal could not see this).

    Returns:
        `(token, subdirs)` -- `subdirs` is every child directory found,
        for the caller's own bounded-depth traversal. Both an absent path
        and an unscannable one collapse to the SAME distinct token shape
        (`{path}:UNAVAILABLE`) with empty `subdirs` -- see
        `_stat_dir_or_none`/`_scan_dir_entries` for the (differently
        logged) reasons why.
    """
    dir_stat = _stat_dir_or_none(path)
    if dir_stat is None:
        return f"{path}:UNAVAILABLE", []

    scan_result = _scan_dir_entries(path)
    if scan_result is None:
        return f"{path}:UNAVAILABLE", []
    file_tokens, subdirs = scan_result

    token = f"{path}:{dir_stat.st_mtime_ns}:{len(file_tokens) + len(subdirs)}:" + (
        "|".join(sorted(file_tokens))
    )
    return token, subdirs


#: Bounded recursion depth for the nested-shard-directory fingerprint
#: (Finding 1, dual code-review round). This project's CLAUDE.md documents
#: the legacy SHARDED_JSON collection layout as "4-level hash-sharded" (one
#: vector_<hash>.json file per chunk, nested under hash-prefix
#: subdirectories). A top-level-only fingerprint (the original
#: implementation) cannot detect an operator deleting/replacing a
#: duplicate file deep inside one of these shard directories -- the exact
#: real remediation for Issue #1477's corruption scenario -- since that
#: never touches the collection root's own mtime/entry-count. Bounding the
#: walk to this KNOWN maximum depth keeps the cost O(directories within 4
#: levels), never O(files in the whole collection): a fleet repo can hold
#: millions of chunk files, but this signature is only ever computed for a
#: candidate that has ALREADY reached the quarantine threshold (a rare,
#: not-every-tick operation on a small number of persistently-failing
#: repos), never on the hot indexing/migration path itself.
_SIGNATURE_MAX_SHARD_DEPTH = 4


def _collect_dir_state_tokens(root: Path, max_depth: int) -> List[str]:
    """Collect a `_dir_state_token` for `root` itself plus every
    subdirectory beneath it, down to `max_depth` levels -- bounded, so a
    change to ANY nested shard directory's own contents (a file added/
    removed/replaced/rewritten-in-place) is reflected without scanning
    every file in a collection that may hold millions of them.

    Finding C item 1 (Codex round-3 review): `_dir_state_token` itself
    already discovers each directory's own subdirectories via its single
    `os.scandir()` pass -- this function reuses that SAME result for its
    own recursion rather than performing a SECOND, separate directory
    read (the round-1/2 `iterdir()` + `is_dir()` pattern this replaces).

    A directory with fewer levels of nesting than `max_depth` (e.g. this
    project's test fixtures, which use a 2-level shard layout) naturally
    terminates early -- no further subdirectories are found to descend
    into, which is not an error.
    """
    tokens: List[str] = []
    frontier: List[Any] = [(root, 0)]
    while frontier:
        current_dir, depth = frontier.pop()
        # _dir_state_token() NEVER raises OSError -- _stat_dir_or_none()
        # and _scan_dir_entries() already catch every OSError internally
        # (logging as appropriate) and return an "UNAVAILABLE" token
        # instead, preserving this loop's original resilience to a
        # directory becoming inaccessible mid-traversal (permission
        # change, stale NFS mount, concurrent deletion) without a
        # try/except at this call site.
        token, subdirs = _dir_state_token(current_dir)
        tokens.append(token)
        if depth >= max_depth:
            continue
        for child in subdirs:
            frontier.append((child, depth + 1))
    return tokens


def compute_repo_state_signature(candidate: FleetMigrationCandidate) -> str:
    """A deterministic fingerprint of a fleet-migration candidate's
    on-disk collection/temporal state, used SOLELY to detect a genuine
    change since a repo was quarantined -- never to decide whether or how
    to consolidate (that remains `consolidate_collection_in_place`'s job
    alone).

    Sensitive to nested hash-shard directory state (Finding 1, dual
    review round) AND in-place file content rewrites (Finding B, Codex
    round-3 review), not just each collection/temporal-shard directory's
    own top-level metadata -- see `_collect_dir_state_tokens`.
    """
    tokens = [_dir_state_token(candidate.index_path)[0]]
    for collection_dir in sorted(candidate.semantic_collection_dirs):
        tokens.extend(
            _collect_dir_state_tokens(collection_dir, _SIGNATURE_MAX_SHARD_DEPTH)
        )
    for namespace in sorted(
        candidate.temporal_namespaces, key=lambda ns: ns.pointer_namespace
    ):
        tokens.extend(
            _collect_dir_state_tokens(
                namespace.legacy_shard_dir, _SIGNATURE_MAX_SHARD_DEPTH
            )
        )
    # Sorted for determinism -- directory-iteration order is not a
    # guaranteed stable contract to rely on across calls.
    combined = "|".join(sorted(tokens))
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def record_migration_failure(
    golden_repo_manager: Any,
    golden_alias: str,
    state_signature: str,
    failure_cause: Optional[str] = None,
) -> None:
    """Record one fleet-migration failure for `golden_alias`, persisted via
    the shared backend. `failure_cause` (Finding I, Codex round-5 review
    -- e.g. `DISK_HEADROOM_FAILURE_CAUSE`/`GENERIC_FAILURE_CAUSE`, see
    `classify_failure_cause()`) is stored so `is_quarantined()` can later
    choose the correct auto-clear strategy.

    Returns normally (a no-op) when no quarantine backend is configured
    at all -- the deliberate "quarantine tracking disabled" state.

    Raises:
        QuarantineStateUnavailableError: Finding D (Codex round-4 review,
            live-reproduced): a backend IS configured but the WRITE
            genuinely FAILED -- never silently swallowed. See
            `QuarantineStateUnavailableError`'s own docstring and Finding
            G (the scheduler's pre-flight health probe) for how the
            scheduler must respond to a PERSISTENT write outage.
    """
    golden_alias = normalize_golden_alias(golden_alias)
    backend = _get_quarantine_backend(golden_repo_manager)
    if backend is None or not hasattr(backend, "record_fleet_migration_failure"):
        return
    try:
        backend.record_fleet_migration_failure(
            golden_alias, state_signature, failure_cause=failure_cause
        )
    except Exception as exc:  # noqa: BLE001 -- re-raised as a typed error below
        logger.error(
            "Issue #1477: failed to PERSIST a fleet-migration failure for "
            "%r -- this is a genuine, actionable backend error. Without "
            "this write, the consecutive-failure count would be silently "
            "understated, defeating quarantine for exactly the "
            "persistently-broken-repo case it exists to handle: %s",
            golden_alias,
            exc,
        )
        raise QuarantineStateUnavailableError(
            f"failed to persist fleet-migration failure for {golden_alias!r}: {exc}"
        ) from exc


def reset_migration_failure(golden_repo_manager: Any, golden_alias: str) -> None:
    """Clear any persisted fleet-migration failure/quarantine state for
    `golden_alias`. A no-op when no quarantine backend is configured at
    all -- the deliberate "tracking disabled" state.

    Raises:
        QuarantineStateUnavailableError: Finding H (Codex round-5
            review, live-reproduced): the backend DELETE genuinely
            failed -- never silently swallowed (a round-4 version
            logged-and-returned, which could report "cleared" when the
            row was never actually deleted). See
            `QuarantineStateUnavailableError`'s own docstring and
            callers (`is_quarantined()`'s auto-clear branch,
            `FleetMigrationScheduler`'s success-reset call) for how each
            handles this.
    """
    golden_alias = normalize_golden_alias(golden_alias)
    backend = _get_quarantine_backend(golden_repo_manager)
    if backend is None or not hasattr(backend, "reset_fleet_migration_failure"):
        return
    try:
        backend.reset_fleet_migration_failure(golden_alias)
    except Exception as exc:  # noqa: BLE001 -- re-raised as a typed error below
        logger.error(
            "Issue #1477: failed to CLEAR the fleet-migration failure "
            "state for %r -- a stale row may persist; the TRUE "
            "persisted state must be re-read, never assumed cleared: %s",
            golden_alias,
            exc,
        )
        raise QuarantineStateUnavailableError(
            f"failed to clear fleet-migration failure state for {golden_alias!r}: {exc}"
        ) from exc


def soft_reset_migration_failure(golden_repo_manager: Any, golden_alias: str) -> None:
    """Zero `consecutive_failure_count` for `golden_alias` WITHOUT
    deleting its row (Finding N, Codex round-7 review, live-reproduced) --
    a fallback used by `_clear_quarantine_after_detected_repair()` when
    the full reset (`reset_migration_failure`, a DELETE) fails but a
    plain UPDATE still works. A no-op when no quarantine backend is
    configured at all -- the deliberate "tracking disabled" state.

    Raises:
        QuarantineStateUnavailableError: the backend UPDATE genuinely
            failed -- never silently swallowed, mirroring
            `reset_migration_failure`'s own contract. The caller
            (`_clear_quarantine_after_detected_repair`) catches this and
            falls back further to logging a WARNING and proceeding
            without resetting anything.
    """
    golden_alias = normalize_golden_alias(golden_alias)
    backend = _get_quarantine_backend(golden_repo_manager)
    if backend is None or not hasattr(
        backend, "soft_reset_fleet_migration_failure_count"
    ):
        return
    try:
        backend.soft_reset_fleet_migration_failure_count(golden_alias)
    except Exception as exc:  # noqa: BLE001 -- re-raised as a typed error below
        logger.error(
            "Issue #1477: failed to SOFT-RESET (zero the failure count "
            "for, without deleting) the fleet-migration state for %r: %s",
            golden_alias,
            exc,
        )
        raise QuarantineStateUnavailableError(
            f"failed to soft-reset fleet-migration failure count for {golden_alias!r}: {exc}"
        ) from exc


def touch_migration_failure_check(golden_repo_manager: Any, golden_alias: str) -> None:
    """Update ONLY the throttle-bookkeeping `signature_checked_at`
    timestamp for `golden_alias` (Finding C item 2, Codex round-3 review)
    -- called by `is_quarantined()` after re-verifying an unchanged
    signature, so the NEXT recheck window starts fresh. Never touches
    `consecutive_failure_count` or `state_signature` (those change ONLY
    via a genuine new failure or an actual detected on-disk change).

    Fail-soft (mirrors `record_migration_failure`/`reset_migration_failure`):
    a backend WRITE failure is logged loudly at ERROR but never raises --
    at worst, the next `is_quarantined()` call simply re-verifies sooner
    than the ideal cadence, which is a performance concern, not a
    correctness one.
    """
    golden_alias = normalize_golden_alias(golden_alias)
    backend = _get_quarantine_backend(golden_repo_manager)
    if backend is None or not hasattr(backend, "touch_fleet_migration_failure_check"):
        return
    try:
        backend.touch_fleet_migration_failure_check(golden_alias)
    except Exception as exc:  # noqa: BLE001 -- best-effort bookkeeping
        logger.error(
            "Issue #1477: failed to update the fleet-migration throttle "
            "bookkeeping timestamp for %r -- the next recheck may happen "
            "sooner than the ideal cadence: %s",
            golden_alias,
            exc,
        )


#: Finding G (Codex round-5 review): a sentinel alias no REAL golden repo
#: will ever collide with, used SOLELY by probe_quarantine_backend_health()
#: below -- the probe's write+cleanup never touches any real repo's
#: quarantine state.
_HEALTH_PROBE_ALIAS = "__fleet_migration_quarantine_health_probe__"


def probe_quarantine_backend_health(golden_repo_manager: Any) -> bool:
    """Cheap, side-effect-safe write+read round-trip against the SAME
    quarantine backend `record_migration_failure()`/`is_quarantined()`
    use (Finding G, Codex round-5 review, live-reproduced) -- reuses the
    EXISTING `record_fleet_migration_failure`/`reset_fleet_migration_failure`
    backend methods rather than inventing a new backend primitive.

    Used by `FleetMigrationScheduler` to detect whether a previously-
    observed WRITE-bookkeeping outage has recovered, BEFORE re-attempting
    the expensive/destructive `run_fleet_migration_for_repo()` call:
    during a persistent write outage, `is_quarantined()`'s READ still
    succeeds (reporting a stale, never-advancing failure count), so
    without this probe the scheduler would keep re-invoking the real
    migration on EVERY tick, not just the cheap bookkeeping call.

    Returns:
        True if no backend is configured at all (a deliberate "tracking
        disabled" state -- not the same as "unhealthy", nothing to
        probe), OR if the WRITE against a throwaway sentinel alias
        succeeded. False on a genuine WRITE failure -- never raises
        (this is a boolean health signal, not a state mutation the
        caller needs to react to structurally).

        The cleanup (reset) step is deliberately BEST-EFFORT and does
        NOT affect this return value: a backend whose RESET/DELETE is
        specifically broken (Finding H) but whose WRITE succeeds is
        still healthy for THIS probe's purpose (Finding G's actual
        concern is "can we record a new failure", never "can we also
        delete a sentinel row"). Conflating the two would make this
        probe report unhealthy whenever only cleanup is broken,
        needlessly blocking migration attempts for a problem this
        probe was never meant to gate.
    """
    backend = _get_quarantine_backend(golden_repo_manager)
    if backend is None or not hasattr(backend, "record_fleet_migration_failure"):
        return True
    try:
        backend.record_fleet_migration_failure(_HEALTH_PROBE_ALIAS, "probe")
    except Exception as exc:  # noqa: BLE001 -- boolean health probe, never raises
        logger.warning(
            "Issue #1477: quarantine backend health probe FAILED -- %s",
            exc,
        )
        return False

    if hasattr(backend, "reset_fleet_migration_failure"):
        try:
            backend.reset_fleet_migration_failure(_HEALTH_PROBE_ALIAS)
        except Exception as cleanup_exc:  # noqa: BLE001 -- best-effort cleanup only
            logger.warning(
                "Issue #1477: quarantine backend health probe's cleanup "
                "(reset) of its own sentinel row failed -- WRITE health "
                "was already confirmed, so the probe still reports "
                "healthy; a stray sentinel row may persist: %s",
                cleanup_exc,
            )
    return True


def get_failure_state(
    golden_repo_manager: Any, golden_alias: str
) -> Optional[Dict[str, Any]]:
    """Return the currently persisted fleet-migration failure state for
    `golden_alias`.

    Returns None when `golden_alias` genuinely has no recorded failures
    (never failed, reset since), OR when no quarantine backend is
    configured at all -- a deliberate "quarantine tracking disabled"
    state (e.g. a test double, or a golden_repo_manager surface that never
    wires `_sqlite_backend`).

    Raises:
        QuarantineStateUnavailableError: Finding A (Codex round-3 review,
            live-reproduced): a backend IS configured but the actual query
            genuinely FAILED (a real table/connection error). This is
            loudly logged at ERROR (Messi Rule #13 Anti-Silent-Failure)
            AND raised -- never silently swallowed into a return value.
            A round-2 version of this function treated a query failure
            the SAME as "confirmed not quarantined" on the reasoning that
            quarantine is a backoff mechanism, not a correctness
            guarantee -- Codex proved that reasoning backwards for THIS
            check with a live repro: a PERSISTENT backend outage made the
            scheduler proceed with the SAME first candidate forever,
            recreating Issue #1477's exact fleet-starvation bug via a
            third path (backend outage, alongside corrupt data and a
            non-raising status). The caller (the scheduler) must catch
            this specifically and abort the scheduling tick instead.
    """
    golden_alias = normalize_golden_alias(golden_alias)
    backend = _get_quarantine_backend(golden_repo_manager)
    if backend is None or not hasattr(backend, "get_fleet_migration_failure_state"):
        return None
    try:
        return backend.get_fleet_migration_failure_state(  # type: ignore[no-any-return]
            golden_alias
        )
    except Exception as exc:  # noqa: BLE001 -- re-raised as a typed error below
        logger.error(
            "Issue #1477: quarantine state for %r is UNAVAILABLE -- "
            "backend read failed: %s. This is a genuine, actionable "
            "backend error -- investigate it.",
            golden_alias,
            exc,
        )
        raise QuarantineStateUnavailableError(
            f"failed to read fleet-migration failure state for {golden_alias!r}: {exc}"
        ) from exc


#: Finding C item 2 (Codex round-3 review): minimum wall-clock gap
#: (seconds) between EXPENSIVE full-signature rechecks of an already-
#: quarantined repo. Codex measured the walk at ~0.5s / ~45,338 stat()
#: calls on a synthetic 21,518-file collection deliberately matching the
#: real "evolution" golden repo's actual scale -- running this on EVERY
#: tick for EVERY already-quarantined candidate the scheduler's loop
#: encounters is a genuine I/O storm on NFS (this project's actual
#: staging/cluster storage). 5 minutes comfortably reduces this to a
#: small, bounded background cost while still detecting a genuine
#: operator repair well within the same order of magnitude as a typical
#: scheduler tick interval.
_SIGNATURE_RECHECK_INTERVAL_SECONDS = 300


def _parse_checked_at(value: Any) -> Optional[datetime]:
    """Normalize a persisted `signature_checked_at` value into a tz-aware
    UTC datetime, mirroring golden_repo_reconciler.py's own
    `_parse_observed_at` convention -- the two backends store this
    differently (SQLite: ISO-8601 string; PostgreSQL: native datetime).
    Returns None on `None` input or an unparseable string -- callers
    treat that as "recheck is due now" (never gates a recheck forever on
    a parse failure)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return (
            parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        )
    return None


def _clear_quarantine_after_detected_repair(
    golden_repo_manager: Any, golden_alias: str, reason: str
) -> None:
    """Attempt to durably clear a quarantine's stale row AFTER genuine
    repair evidence has ALREADY been detected via a successful READ
    (Finding K, Codex round-6 review, live-reproduced): e.g. a changed
    directory-content signature, or disk headroom now confirmed
    sufficient.

    A broken RESET/DELETE here must NEVER abort the caller
    (`is_quarantined()`) or block migration -- the READ already
    succeeded and told us this repo is no longer failing; re-raising
    here would make a reset/delete-only outage block the entire fleet,
    directly contradicting Finding H's own principle ("a broken reset
    must never block migration").

    Finding N (Codex round-7 review, live-reproduced): a full-reset
    failure alone is only PARTIALLY self-healing -- `record_migration_
    failure` never resets `consecutive_failure_count` back to 1, it just
    increments the STALE elevated count. A just-repaired repo that then
    legitimately fails once more for an unrelated reason would
    immediately re-quarantine, never getting its intended fresh 3-attempt
    budget. So when the full reset (DELETE) fails, a SOFT reset (UPDATE
    consecutive_failure_count = 0, keeping the row) is attempted as a
    fallback -- a DIFFERENT SQL operation that can succeed even when
    DELETE specifically cannot (the same asymmetry Finding H/K's whole
    premise was built on). Only if BOTH fail is the deepest fallback
    used: log a WARNING and proceed without resetting anything -- at
    that point genuinely nothing writable works, a scenario the
    SEPARATE write-health probe (Finding G/J) independently catches and
    blocks on the NEXT tick's migration attempt anyway, bounding this
    residual rather than creating a new starvation vector.
    """
    try:
        reset_migration_failure(golden_repo_manager, golden_alias)
        return
    except QuarantineStateUnavailableError as reset_exc:
        logger.warning(
            "Issue #1477: repo %r's quarantine repair was detected (%s), "
            "but the full reset (DELETE) of the stale row failed (%s) -- "
            "attempting a soft reset (zeroing consecutive_failure_count "
            "while keeping the row) so a fresh failure budget is still "
            "restored.",
            golden_alias,
            reason,
            reset_exc,
        )

    try:
        soft_reset_migration_failure(golden_repo_manager, golden_alias)
    except QuarantineStateUnavailableError as soft_reset_exc:
        logger.warning(
            "Issue #1477: repo %r's quarantine repair was detected (%s), "
            "but BOTH the full reset (DELETE) and the soft-reset "
            "(UPDATE) fallback failed (%s) -- proceeding as NOT "
            "quarantined anyway (the READ already confirmed the repair; "
            "a broken write path must never block migration). The "
            "stale, elevated failure count remains until a write path "
            "recovers; the separate write-health probe (Finding G/J) "
            "will independently catch and block on the next tick if "
            "writes are still broken.",
            golden_alias,
            reason,
            soft_reset_exc,
        )


def is_quarantined(
    golden_repo_manager: Any,
    candidate: FleetMigrationCandidate,
    *,
    threshold: int = FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD,
    recheck_interval_seconds: Optional[float] = None,
) -> bool:
    """True iff `candidate.golden_alias` has reached the consecutive
    failure quarantine threshold AND the on-disk state has not genuinely
    changed since the last recorded failure.

    Mirrors `description_refresh_scheduler.py`'s commit-based auto-clear
    gate (Bug #1096's lesson: a bare retry must never look like evidence
    of change -- defeating quarantine for exactly the worst, persistently-
    broken-repo case it exists to handle).

    A quarantine that IS auto-cleared here (genuine on-disk state change
    detected) has its persisted failure state reset as a side effect, so
    the caller is free to attempt migration again without a separate
    cleanup step.

    Finding C item 2 (Codex round-3 review): the EXPENSIVE full-signature
    recomputation (now leaf-file-stat-inclusive, per Finding B) is
    THROTTLED to at most once per `recheck_interval_seconds` -- within
    that window, the cached quarantined=True state is returned with NO
    directory walk at all. The throttle ONLY controls WHEN the expensive
    check runs; it NEVER by itself clears quarantine -- clearing still
    requires an ACTUAL detected on-disk signature difference, exactly as
    before. A missing/unparseable `signature_checked_at` is treated as
    "recheck is due now" (never gates a recheck forever on a bookkeeping
    gap).

    `recheck_interval_seconds=None` (the default -- every real production
    caller, including `FleetMigrationScheduler`) resolves the module-level
    `_SIGNATURE_RECHECK_INTERVAL_SECONDS` constant AT CALL TIME, not as a
    bound function-default -- this is deliberate: it lets tests
    monkeypatch that constant to affect callers that rely on the default,
    which a bound default (evaluated once at function-definition/import
    time) cannot do.

    Raises:
        ValueError: the resolved interval is negative.
    """
    effective_interval_seconds = (
        recheck_interval_seconds
        if recheck_interval_seconds is not None
        else _SIGNATURE_RECHECK_INTERVAL_SECONDS
    )
    if effective_interval_seconds < 0:
        raise ValueError(
            f"recheck_interval_seconds must be >= 0, got {effective_interval_seconds!r}"
        )

    state = get_failure_state(golden_repo_manager, candidate.golden_alias)
    if state is None:
        return False
    if int(state.get("consecutive_failure_count", 0)) < threshold:
        return False

    if state.get("failure_cause") == DISK_HEADROOM_FAILURE_CAUSE:
        # Finding I (Codex round-5 review): a disk-headroom-caused
        # quarantine clears via the SAME oracle orchestrator.py's
        # preflight uses, INDEPENDENT of the directory-content signature
        # (which cannot observe disk space freed up elsewhere on the
        # filesystem). statvfs is a single cheap syscall -- unlike the
        # nested-directory content walk Finding C throttles, no recheck
        # cadence is needed here.
        if _disk_headroom_currently_sufficient(candidate):
            logger.info(
                "Issue #1477: repo %r was quarantined for insufficient "
                "disk headroom, and the SAME disk-headroom oracle now "
                "reports sufficient space -- clearing quarantine "
                "independent of directory content.",
                candidate.golden_alias,
            )
            _clear_quarantine_after_detected_repair(
                golden_repo_manager,
                candidate.golden_alias,
                reason="disk headroom now sufficient",
            )
            return False
        logger.debug(
            "Issue #1477: repo %r remains quarantined -- the disk-headroom "
            "oracle still reports insufficient space.",
            candidate.golden_alias,
        )
        return True

    checked_at = _parse_checked_at(state.get("signature_checked_at"))
    if checked_at is not None:
        elapsed_seconds = (datetime.now(timezone.utc) - checked_at).total_seconds()
        if elapsed_seconds < effective_interval_seconds:
            logger.debug(
                "Issue #1477: repo %r is quarantined (%s consecutive "
                "failures >= threshold %d) -- within the %.1fs recheck "
                "throttle window (last checked %.1fs ago), skipping "
                "WITHOUT recomputing the expensive signature.",
                candidate.golden_alias,
                state.get("consecutive_failure_count"),
                threshold,
                effective_interval_seconds,
                elapsed_seconds,
            )
            return True

    current_signature = compute_repo_state_signature(candidate)
    if current_signature != state.get("state_signature"):
        logger.info(
            "Issue #1477: repo %r was quarantined but its on-disk state "
            "has genuinely changed since the last recorded failure -- "
            "clearing quarantine and allowing a retry.",
            candidate.golden_alias,
        )
        _clear_quarantine_after_detected_repair(
            golden_repo_manager,
            candidate.golden_alias,
            reason="directory-content signature changed",
        )
        return False

    # Unchanged -- update ONLY the throttle-bookkeeping timestamp so the
    # NEXT recheck window starts fresh from here (never touches the
    # failure count or stored signature).
    touch_migration_failure_check(golden_repo_manager, candidate.golden_alias)

    logger.debug(
        "Issue #1477: repo %r is quarantined (%s consecutive failures >= "
        "threshold %d, on-disk state unchanged after a full recheck) -- "
        "skipping.",
        candidate.golden_alias,
        state.get("consecutive_failure_count"),
        threshold,
    )
    return True


def count_quarantined(
    golden_repo_manager: Any,
    golden_aliases: Iterable[str],
    *,
    threshold: int = FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD,
) -> int:
    """Count how many of `golden_aliases` are CURRENTLY quarantined
    (persisted consecutive failure count at/above `threshold`) -- used by
    `FleetMigrationScheduler.get_stats()` for dashboard visibility.

    Bug #1486 Defect D: rows whose `failure_cause` is
    `UNRECOVERABLE_FAILURE_CAUSE` are EXCLUDED from this count -- they are
    reported by :func:`count_unrecoverable` instead, and the two dashboard
    categories must be mutually exclusive. Because
    `record_unrecoverable_corruption()` reuses the SAME
    consecutive-failure counter/table (just with a distinct failure cause),
    a repo that quarantined FIRST and only later revealed permanent
    corruption would otherwise satisfy BOTH the count>=threshold predicate
    here AND the cause==UNRECOVERABLE predicate in
    :func:`count_unrecoverable`, double-counting it and producing an
    inconsistent dashboard (e.g. pending=0, quarantined=1, unrecoverable=1
    for a single repo).

    Deliberately count-only (no state-change auto-clear side effect) --
    that only happens on an actual scheduling attempt, via
    :func:`is_quarantined`.
    """
    backend = _get_quarantine_backend(golden_repo_manager)
    if backend is None or not hasattr(backend, "list_fleet_migration_failure_states"):
        return 0
    alias_set = set(golden_aliases)
    try:
        rows: List[Dict[str, Any]] = backend.list_fleet_migration_failure_states()
    except Exception as exc:  # noqa: BLE001 -- best-effort bookkeeping
        logger.error(
            "Issue #1477: failed to read fleet-migration failure states for stats: %s",
            exc,
        )
        return 0
    return sum(
        1
        for row in rows
        if row.get("golden_alias") in alias_set
        and int(row.get("consecutive_failure_count", 0)) >= threshold
        and row.get("failure_cause") != UNRECOVERABLE_FAILURE_CAUSE
    )


def count_unrecoverable(golden_repo_manager: Any, golden_aliases: Iterable[str]) -> int:
    """Bug #1486 High Finding 4: count how many of `golden_aliases` are
    recorded with the PERMANENT `UNRECOVERABLE_FAILURE_CAUSE` -- used by
    `FleetMigrationScheduler.get_stats()` for dashboard visibility,
    mirroring `count_quarantined()`'s exact fail-open pattern (a backend
    read failure or missing backend returns 0, never raises -- this is
    a best-effort stats surface, not a correctness-critical decision).
    """
    backend = _get_quarantine_backend(golden_repo_manager)
    if backend is None or not hasattr(backend, "list_fleet_migration_failure_states"):
        return 0
    alias_set = set(golden_aliases)
    try:
        rows: List[Dict[str, Any]] = backend.list_fleet_migration_failure_states()
    except Exception as exc:  # noqa: BLE001 -- best-effort bookkeeping
        logger.error(
            "Bug #1486: failed to read fleet-migration failure states for stats: %s",
            exc,
        )
        return 0
    return sum(
        1
        for row in rows
        if row.get("golden_alias") in alias_set
        and row.get("failure_cause") == UNRECOVERABLE_FAILURE_CAUSE
    )


#: Finding 2 (Codex-found, dual review round): `orchestrator.py`'s
#: `FleetMigrationRepoResult.status` values that mean "someone/something
#: else is legitimately using this repo right now, try again next tick" --
#: NOT "this repo made no progress and won't on a bare retry". These must
#: NEVER count toward the quarantine breaker, since they resolve on their
#: own without any repo-level remediation.
_QUARANTINE_EXEMPT_TRANSIENT_STATUSES = frozenset(
    {
        "lock_held",
        "refresh_in_flight",
        # Codex review Finding F3 / Design decision 7: a duplicate-
        # point-id group detected while the Story #1460 rollout gate is
        # closed (deletion_authorized=False) resolves on its own once
        # the operator opens the gate -- it must NEVER quarantine,
        # unconditionally, regardless of how many ticks it recurs.
        "dedup_deletion_gated",
    }
)


def status_counts_as_quarantine_failure(status: str) -> bool:
    """True iff a NON-RAISING `FleetMigrationRepoResult.status` value
    represents "no progress was made and a bare retry won't help" --
    counted toward the SAME consecutive-failure quarantine counter an
    exception increments.

    This is an EXPLICIT classification (never an implicit fallthrough
    based on "not completed"): "completed" and the transient statuses in
    `_QUARANTINE_EXEMPT_TRANSIENT_STATUSES` are the only statuses that do
    NOT count. Every other status -- including "incomplete" (e.g. a
    persistent disk-space skip), "refused_immutable_path", and any future,
    not-yet-invented status -- counts BY DEFAULT (fail-conservative). This
    is deliberately the safe default direction: failing to count a
    genuine no-progress status is exactly what reproduces Issue #1477's
    fleet-starvation bug via a non-exception path (a repo whose migration
    returns "incomplete" every tick without ever raising was previously
    invisible to the breaker entirely). A future new orchestrator status
    is therefore quarantined by default until explicitly added to the
    exempt set above, rather than silently bypassing the breaker again.
    """
    if status == "completed":
        return False
    if status in _QUARANTINE_EXEMPT_TRANSIENT_STATUSES:
        return False
    return True


#: Finding I (Codex round-5 review): persisted failure-cause values. A
#: directory-content signature alone cannot observe disk space freed up
#: ELSEWHERE on the filesystem, so a disk-headroom-caused quarantine needs
#: an INDEPENDENT clearing path -- re-evaluating the SAME disk-headroom
#: oracle `storage/shared/collection_migration.py`'s
#: `consolidate_collection_in_place()` preflight already uses, rather than
#: reinventing a parallel disk-space heuristic.
DISK_HEADROOM_FAILURE_CAUSE = "disk_headroom"
GENERIC_FAILURE_CAUSE = "generic"

#: Bug #1486 Fix C: a distinct, PERMANENT terminal failure-cause value --
#: chunks.db is genuinely corrupt/unopenable AND at least one previously-
#: migrated record's legacy source is already gone (raised by
#: collection_migration.py as UnrecoverableConsolidationCorruptionError).
#: Unlike DISK_HEADROOM_FAILURE_CAUSE/GENERIC_FAILURE_CAUSE, a repo
#: recorded with this cause is NEVER auto-cleared by is_quarantined()'s
#: directory-content-signature check -- permanent data loss has no
#: signature that could ever change back to "recoverable". It clears
#: ONLY via an explicit reset_migration_failure() call (e.g. after a
#: verified manual restore/reindex -- Bug #1486 Fix D, out of scope for
#: this module).
UNRECOVERABLE_FAILURE_CAUSE = "unrecoverable_corruption"

#: Bug #1579: a distinct failure-cause value for a collection the
#: whole-collection identity gate REJECTED while genuine duplicate
#: point_id group(s) were present (`consolidate_collection_in_place`'s
#: `"dedup_gate_rejected"` status). Unlike `GENERIC_FAILURE_CAUSE`, this
#: is a CONFIRMED, specific diagnosis: a bare retry cannot possibly
#: succeed until the gate-breaking record (missing/foreign `unique_key`)
#: is fixed by a human or an unrelated re-index. Deliberately excluded
#: from `reset_duplicate_caused_quarantine_if_resolved`'s scope (see
#: that function) -- resetting a repo known to be stuck for THIS reason
#: would reset, immediately re-attempt, immediately re-fail identically,
#: and re-quarantine on every tick, hogging the scheduler and starving
#: every alphabetically-later fleet-migration candidate (Bug #1477's
#: starvation failure mode). Clears only via manual review / an explicit
#: reset, exactly like UNRECOVERABLE_FAILURE_CAUSE.
DEDUP_GATE_REJECTED_FAILURE_CAUSE = "dedup_gate_rejected"


def record_unrecoverable_corruption(
    golden_repo_manager: Any, golden_alias: str, detail: str
) -> None:
    """Bug #1486 Fix C: durably record `golden_alias` as PERMANENTLY
    unrecoverable -- a terminal state, distinct from the ordinary
    consecutive-failure quarantine (which auto-clears on a genuine
    on-disk state change). Data corruption with no remaining legacy
    source can never be "fixed" by a bare retry or even a directory
    content change (the lost bytes are gone forever); only explicit
    operator action (a verified manual restore/reindex, Fix D) can clear
    this.

    Reuses the SAME backend/table `record_migration_failure()` already
    writes to (no new storage mechanism) via the dedicated
    `UNRECOVERABLE_FAILURE_CAUSE` value -- `detail` (the exception
    message) is stored as the `state_signature`, purely for diagnostic
    visibility (never compared against a recomputed directory signature,
    since `is_permanently_unrecoverable()` never auto-clears on that
    basis).

    Raises:
        QuarantineStateUnavailableError: Bug #1486 High Finding 5 -- the
            backend WRITE genuinely failed, OR no quarantine backend is
            configured/capable at all. Unlike ordinary
            `record_migration_failure()` (which deliberately no-ops for
            the "tracking disabled" case -- acceptable there since a
            missed ordinary quarantine bookkeeping write just means one
            fewer consecutive-failure count, self-correcting on the next
            failure), THIS specific record must FAIL CLOSED: silently
            no-op'ing here would let the scheduler believe the terminal
            state was durably persisted and retry the exact same doomed
            repo forever -- reproducing Bug #1486's whole incident via a
            missing-backend path instead of a corrupt-data path.
    """
    backend = _get_quarantine_backend(golden_repo_manager)
    if backend is None or not hasattr(backend, "record_fleet_migration_failure"):
        raise QuarantineStateUnavailableError(
            f"Bug #1486 High Finding 5: cannot durably record "
            f"{golden_alias!r} as PERMANENTLY unrecoverable -- no "
            f"quarantine backend is configured (or it lacks "
            f"record_fleet_migration_failure). This must fail closed: "
            f"the caller (FleetMigrationScheduler) must abort this "
            f"scheduling tick rather than silently proceeding as if the "
            f"terminal state had been persisted."
        )
    record_migration_failure(
        golden_repo_manager,
        golden_alias,
        detail,
        failure_cause=UNRECOVERABLE_FAILURE_CAUSE,
    )


def is_permanently_unrecoverable(golden_repo_manager: Any, golden_alias: str) -> bool:
    """Bug #1486 Fix C: True iff `golden_alias` is recorded with the
    PERMANENT `UNRECOVERABLE_FAILURE_CAUSE`.

    Unlike `is_quarantined()`, this NEVER auto-clears via a directory-
    content-signature check -- permanent data loss has no signature that
    could ever change back to "recoverable". It clears ONLY via an
    explicit `reset_migration_failure()` call.

    Returns False when `golden_alias` has no recorded failure at all, or
    when its recorded failure cause is anything other than
    `UNRECOVERABLE_FAILURE_CAUSE`.

    Raises:
        QuarantineStateUnavailableError: propagated from
            `get_failure_state()` on a genuine backend READ failure --
            callers must treat this the SAME way they already treat
            `is_quarantined()`'s own read failures (abort the tick,
            never silently assume "not unrecoverable").
    """
    state = get_failure_state(golden_repo_manager, golden_alias)
    if state is None:
        return False
    return bool(state.get("failure_cause") == UNRECOVERABLE_FAILURE_CAUSE)


def reset_duplicate_caused_quarantine_if_resolved(
    golden_repo_manager: Any,
    candidate: FleetMigrationCandidate,
    *,
    threshold: int = FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD,
) -> bool:
    """Story #1560 AC12: explicit, durable pre-attempt reset for a repo
    already quarantined by a duplicate-point-id cause -- called BEFORE
    `is_quarantined()`, since that check's own signature auto-clear can
    never fire for this cause (repair never runs on an already-skipped
    candidate, so nothing ever changes the signature). Skips repos
    below `threshold` or quarantined for `UNRECOVERABLE_FAILURE_CAUSE`
    (permanent data loss has no duplicate-resolution fix).

    Resets WHEN `collection_has_any_duplicate_point_ids()` finds a
    duplicate STILL present -- this is intentional, not inverted: prior
    to this story, a duplicate point_id made the repair step raise
    `DuplicateSourceIdError`, which is why the repo was quarantined in
    the first place. That repair step no longer raises for this
    condition -- it auto-resolves it by deletion. So finding a
    duplicate still present is exactly the signal that this repo's
    STUCK quarantine is explained by a cause the CODE now safely
    handles; the DATA itself is resolved on the very next migration
    attempt this reset unblocks, not before.

    Bug #1579: this MUST use the GATE-AGNOSTIC
    `collection_has_any_duplicate_point_ids`, never the gate-aware
    `collection_has_duplicate_point_ids` (scoped to "duplicates THIS
    REPAIR WOULD AUTO-RESOLVE"). A collection can carry a genuine
    duplicate point_id pair AND, elsewhere in the same collection, one
    unrelated record whose unique_key is missing/foreign -- which makes
    the whole-collection identity gate reject the ENTIRE collection. The
    gate-aware predicate reports "no duplicate" for that shape even
    though one genuinely exists, so this reset could never fire and the
    repo stayed quarantined forever -- unlike every other cause this
    module handles. Using the gate-agnostic predicate means this reset
    ALSO fires for that case, giving the repo a fair renewed retry. That
    retry is bounded and non-silent, never an infinite reset loop that
    pretends to succeed: post Bug #1579 Part 3,
    `consolidate_collection_in_place` returns the honest, distinguishable
    "dedup_gate_rejected" status again (instead of crashing) if the
    underlying data/schema issue is still unresolved, and that status
    counts toward quarantine again via the normal fail-conservative
    default (`status_counts_as_quarantine_failure`) -- so a genuinely
    unresolvable repo re-quarantines rather than looping here forever.

    Reuses `collection_has_any_duplicate_point_ids` (read-only) and
    `_clear_quarantine_after_detected_repair` (existing reset/fallback
    logic) rather than reimplementing either. Returns True iff reset.

    Raises:
        QuarantineStateUnavailableError: propagated from
            `get_failure_state()` on a genuine backend READ failure.
    """
    state = get_failure_state(golden_repo_manager, candidate.golden_alias)
    if state is None:
        return False
    # Codex finding F5: a permanently-unrecoverable OR a disk-headroom
    # quarantine has nothing to do with duplicate-point-id resolution --
    # neither is fixed by deleting duplicates, so this reset must never
    # unblock either. is_quarantined() already has its own correct
    # disk-headroom auto-clear (based on the preflight re-passing); this
    # function must not duplicate or override that separate mechanism.
    #
    # Bug #1579: a DEDUP_GATE_REJECTED_FAILURE_CAUSE quarantine is a
    # CONFIRMED gate rejection -- unlike a bare GENERIC-cause quarantine
    # (which might be a legacy crash whose collection would now pass the
    # gate), this cause means we already KNOW a bare retry will fail
    # identically. Resetting it anyway would reset, immediately
    # re-attempt, immediately re-fail, and re-quarantine on every single
    # tick -- hogging the scheduler and starving every alphabetically-
    # later fleet-migration candidate (Bug #1477's starvation failure
    # mode, empirically reproduced while building this fix). Excluded
    # here exactly like the two causes above; clears only via manual
    # review after the underlying data/schema issue is fixed.
    if state.get("failure_cause") in (
        UNRECOVERABLE_FAILURE_CAUSE,
        DISK_HEADROOM_FAILURE_CAUSE,
        DEDUP_GATE_REJECTED_FAILURE_CAUSE,
    ):
        return False
    if int(state.get("consecutive_failure_count", 0)) < threshold:
        return False

    from code_indexer.storage.shared.collection_dedup_repair import (
        collection_has_any_duplicate_point_ids,
    )

    for collection_dir in candidate.semantic_collection_dirs:
        if collection_has_any_duplicate_point_ids(collection_dir):
            logger.info(
                "Story #1560/Bug #1579: repo %r is quarantined and %s "
                "currently has duplicate point_id(s) (gate-agnostic "
                "detection) -- this is now a resolvable cause (auto-"
                "delete, no longer a raised exception) -- resetting "
                "quarantine so the next attempt can succeed.",
                candidate.golden_alias,
                collection_dir,
            )
            _clear_quarantine_after_detected_repair(
                golden_repo_manager,
                candidate.golden_alias,
                reason="duplicate point_id cause now auto-resolvable (Story #1560)",
            )
            return True
    return False


#: Substring `orchestrator.py`'s `_run_migration_sequence()` uses verbatim
#: in `FleetMigrationRepoResult.detail` when a semantic collection was
#: skipped for insufficient disk headroom -- the ONLY non-raising
#: "incomplete" cause this project's fleet-migration pipeline currently
#: produces that maps to `DISK_HEADROOM_FAILURE_CAUSE`.
_DISK_HEADROOM_DETAIL_MARKER = "insufficient disk headroom"

#: Bug #1579: substring `orchestrator.py`'s `_run_migration_sequence()`
#: uses verbatim in `FleetMigrationRepoResult.detail` for the
#: `"dedup_gate_rejected"` status (the whole-collection identity gate
#: rejected a collection while genuine duplicate point_id group(s) were
#: present).
_DEDUP_GATE_REJECTED_DETAIL_MARKER = "rejected by the whole-collection identity gate"


def classify_failure_cause(*, detail: Optional[str] = None) -> str:
    """Classify a fleet-migration failure's cause (Finding I, Codex
    round-5 review) from the orchestrator result's `detail` text.

    An EXCEPTION-path failure (e.g. genuinely corrupt legacy data --
    `detail=None`, since an exception carries no `FleetMigrationRepoResult
    .detail`) is always `GENERIC_FAILURE_CAUSE`. A non-raising
    "incomplete" status whose `detail` names the disk-headroom skip is
    `DISK_HEADROOM_FAILURE_CAUSE`; one whose `detail` names the Bug
    #1579 whole-collection identity gate rejection is
    `DEDUP_GATE_REJECTED_FAILURE_CAUSE` (this constant, already defined
    above alongside `UNRECOVERABLE_FAILURE_CAUSE`); every other detail
    (including "residual in-repo temporal directories remain") is
    `GENERIC_FAILURE_CAUSE`.
    """
    if detail and _DISK_HEADROOM_DETAIL_MARKER in detail:
        return DISK_HEADROOM_FAILURE_CAUSE
    if detail and _DEDUP_GATE_REJECTED_DETAIL_MARKER in detail:
        return DEDUP_GATE_REJECTED_FAILURE_CAUSE
    return GENERIC_FAILURE_CAUSE


def _disk_headroom_currently_sufficient(candidate: FleetMigrationCandidate) -> bool:
    """Re-evaluate disk headroom for `candidate`'s semantic collections
    using the SAME oracle `storage/shared/collection_migration.py`'s
    `consolidate_collection_in_place()` preflight already uses (Finding I,
    Codex round-5 review) -- reused, never reinvented as a parallel
    disk-space heuristic.

    True iff EVERY semantic collection directory reports sufficient
    headroom right now (conservative: any one still-insufficient
    collection means the repo is not yet ready to retry). A repo with no
    semantic collections at all is trivially "sufficient" (nothing to
    check).
    """
    from code_indexer.storage.shared.collection_migration import (
        _estimate_bytes_needed,
        _has_disk_headroom,
    )

    for collection_dir in candidate.semantic_collection_dirs:
        json_paths = list(collection_dir.rglob("vector_*.json"))
        estimated_bytes = _estimate_bytes_needed(json_paths)
        if not _has_disk_headroom(collection_dir, estimated_bytes):
            return False
    return True


def _chunk_stores_now_read_cleanly(candidate: FleetMigrationCandidate) -> bool:
    """Bug #1564: True iff EVERY semantic collection directory for
    `candidate` currently resolves to the consolidated CHUNKS_DB layout
    AND reads real, committed chunk rows via the read-only, never-
    creates-a-file `chunk_store_has_real_data()` primitive (Issue #1459's
    own established read-only inspection contract, `on_error=
    "treat_absent"` so a locked/corrupt store degrades to False rather
    than raising) -- positive, on-disk proof that a previously-recorded
    quarantine condition (corrupt chunks.db, or a still-incomplete
    migration) no longer holds.

    A candidate with NO semantic collections at all is conservatively
    NOT treated as healthy (vacuously true would wrongly clear a
    quarantine for a repo whose collections vanished rather than
    recovered) -- a repo that no longer exists at all is instead reaped
    by the alias-liveness check in `reconcile_stale_quarantine_rows()`;
    a repo that still exists but lost its collections is left
    quarantined here rather than guessed at.
    """
    if not candidate.semantic_collection_dirs:
        return False
    for collection_dir in candidate.semantic_collection_dirs:
        if resolve_chunk_layout(collection_dir) != ChunkLayout.CHUNKS_DB:
            return False
        db_path = collection_dir / "chunks.db"
        if not chunk_store_has_real_data(db_path, on_error="treat_absent"):
            return False
    return True


def _reconcile_one_quarantine_row(
    golden_repo_manager: Any,
    row: Dict[str, Any],
    *,
    live_aliases: Set[str],
    candidates_by_alias: Dict[str, FleetMigrationCandidate],
    threshold: int,
) -> None:
    """Reconcile exactly one persisted quarantine row (Bug #1564) --
    isolated in its own try/except so a fault reconciling ONE row (a
    corrupt/locked store, an unexpected exception) can never prevent
    every OTHER row from being reconciled, and can never propagate up
    into a resilient /health call.
    """
    try:
        alias = row.get("golden_alias")
        if not alias or alias == _HEALTH_PROBE_ALIAS:
            return
        normalized_alias = normalize_golden_alias(alias)

        if normalized_alias not in live_aliases:
            # Gap 3: a row for a golden_alias that is no longer a
            # registered golden repo at all -- reaped regardless of
            # failure_cause/consecutive_failure_count, since a dangling
            # row is garbage no matter how many times it "failed".
            logger.info(
                "Bug #1564: reaping fleet-migration quarantine row for "
                "%r -- it is no longer a registered golden repo.",
                alias,
            )
            try:
                reset_migration_failure(golden_repo_manager, alias)
            except QuarantineStateUnavailableError as exc:
                logger.warning(
                    "Bug #1564: failed to reap quarantine row for %r: %s",
                    alias,
                    exc,
                )
            return

        failure_cause = row.get("failure_cause")
        if failure_cause == DISK_HEADROOM_FAILURE_CAUSE:
            # Already has its own independent, correct auto-clear oracle
            # inside is_quarantined() (_disk_headroom_currently_
            # sufficient) -- never duplicate it here.
            return
        if (
            failure_cause != UNRECOVERABLE_FAILURE_CAUSE
            and int(row.get("consecutive_failure_count", 0)) < threshold
        ):
            # Not yet quarantined by any consumer's definition -- an
            # ordinary retry will resolve this on its own. This gate is
            # deliberately SKIPPED for UNRECOVERABLE_FAILURE_CAUSE --
            # mirroring is_permanently_unrecoverable()'s own binary,
            # non-count-driven contract: record_unrecoverable_corruption()
            # records exactly ONE failure, which never reaches the
            # ordinary quarantine threshold on its own.
            return

        candidate = candidates_by_alias.get(normalized_alias)
        if candidate is None:
            # Cannot currently resolve this repo's on-disk state (e.g. a
            # transiently-unresolvable clone path) -- no positive
            # evidence either way, leave the row untouched.
            return

        if not _chunk_stores_now_read_cleanly(candidate):
            return

        # Gap 1 (UNRECOVERABLE_FAILURE_CAUSE never auto-clears on its
        # own) and Gap 2 (a GENERIC-cause quarantine whose repo has
        # since been repaired is never re-attempted, because the
        # scheduler only re-checks the candidate it is CURRENTLY
        # considering) both close here: positive on-disk evidence of
        # health clears the row so the next attempt/health-check
        # observes reality instead of a stale failure.
        logger.info(
            "Bug #1564: repo %r's quarantine (cause=%s) shows positive "
            "on-disk evidence of health (chunks.db reads cleanly with "
            "real committed rows) -- clearing quarantine.",
            alias,
            failure_cause,
        )
        _clear_quarantine_after_detected_repair(
            golden_repo_manager,
            alias,
            reason="Bug #1564: positive chunk-store health re-validation",
        )
    except Exception as exc:  # noqa: BLE001 -- isolate faults per-row
        logger.warning(
            "Bug #1564: quarantine reconciliation for row %r failed "
            "unexpectedly -- leaving it untouched: %s",
            row.get("golden_alias"),
            exc,
        )


def reconcile_stale_quarantine_rows(
    golden_repo_manager: Any,
    *,
    threshold: int = FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD,
) -> None:
    """Bug #1564: proactively re-validate and reap stale fleet-migration
    quarantine rows.

    Three independent staleness gaps, all closed here:

    1. `UNRECOVERABLE_FAILURE_CAUSE` never auto-clears via
       `is_quarantined()` by design (`record_unrecoverable_corruption()`'s
       own documented contract) -- a manually/out-of-band repaired
       chunks.db stayed permanently reported on /health forever with no
       way to observe the repair.
    2. A GENERIC-cause quarantine whose repo has since been repaired
       never gets re-attempted, because the scheduler only ever re-checks
       the ONE candidate it is CURRENTLY considering (the first not-yet-
       migrated repo, alias-sorted) -- an already-passed-over candidate's
       stale row just sits there, generating WARNING noise every tick.
    3. A row for a golden_alias that is no longer a registered golden
       repo at all is never reaped -- deleting a golden repo leaves its
       quarantine state behind forever.

    This is called from `health_service.py` before it reports
    quarantine-derived /health signals, but the effect is durable and
    backend-shared: clearing/reaping a row here is immediately visible to
    `is_quarantined()`/`is_permanently_unrecoverable()` on their NEXT
    call (including `FleetMigrationScheduler`'s own next tick), closing
    the "resolution observed too late" loop WITHOUT this module (or
    health_service.py) needing to talk to the scheduler directly.

    Deliberately excludes `DISK_HEADROOM_FAILURE_CAUSE` rows from the
    re-validation branch -- that cause already has its own independent,
    correct auto-clear oracle inside `is_quarantined()`
    (`_disk_headroom_currently_sufficient`); duplicating it here would
    risk the two oracles disagreeing. Story #1560's
    `reset_duplicate_caused_quarantine_if_resolved` is likewise untouched
    -- this is a separate, general re-validation/reaping path.

    Fail-open and NEVER raises: this function backs a resilient /health
    reporting surface. Every read/write against the persisted backend,
    the live golden-repo list, and each row's on-disk re-validation is
    independently guarded so that ANY single failure (a locked/corrupt
    store, a backend outage, an unexpected exception) degrades to
    "leave this row alone" rather than crashing the health check, or
    reaping/clearing based on missing evidence (Messi Rule #13: anti-
    silent-failure in spirit, applied conservatively -- absence of proof
    of a problem is never treated as proof of health).
    """
    try:
        backend = _get_quarantine_backend(golden_repo_manager)
        if backend is None or not hasattr(
            backend, "list_fleet_migration_failure_states"
        ):
            return

        try:
            rows = backend.list_fleet_migration_failure_states()
        except Exception as exc:
            logger.debug(
                "Bug #1564: quarantine reconciliation skipped -- could not "
                "list persisted quarantine rows: %s",
                exc,
            )
            return
        if not rows:
            return

        try:
            live_aliases: Set[str] = {
                normalize_golden_alias(alias)
                for entry in golden_repo_manager.list_golden_repos()
                for alias in (entry.get("alias") or entry.get("alias_name"),)
                if alias
            }
        except Exception as exc:
            logger.debug(
                "Bug #1564: quarantine reconciliation skipped -- could not "
                "list live golden repos: %s",
                exc,
            )
            return

        try:
            candidates_by_alias: Dict[str, FleetMigrationCandidate] = {
                normalize_golden_alias(candidate.golden_alias): candidate
                for candidate in enumerate_fleet_migration_candidates(
                    golden_repo_manager
                )
            }
        except Exception as exc:
            logger.debug(
                "Bug #1564: quarantine reconciliation could not enumerate "
                "on-disk candidates -- reaping-by-alias will still run, "
                "but positive-health re-validation will be skipped this "
                "pass: %s",
                exc,
            )
            candidates_by_alias = {}

        for row in rows:
            _reconcile_one_quarantine_row(
                golden_repo_manager,
                row,
                live_aliases=live_aliases,
                candidates_by_alias=candidates_by_alias,
                threshold=threshold,
            )
    except Exception as exc:  # pragma: no cover -- defense in depth
        logger.warning(
            "Bug #1564: quarantine reconciliation aborted unexpectedly: %s",
            exc,
        )
