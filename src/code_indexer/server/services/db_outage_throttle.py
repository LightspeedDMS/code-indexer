"""Shared DB-outage error-storm throttle for background loops (Bug #1249).

When the shared PostgreSQL server briefly restarts/drops, every DB-dependent
background loop in cidx-server (refresh_scheduler, leader_election_service,
node_metrics_writer_service, node_heartbeat_service, config_service) used to
log a fresh ERROR + full traceback on EVERY tick with no backoff and no
dedup. A single ~5 minute PG outage produced ~37k near-identical ERROR rows
across these loops, re-flooding logs.db (the same contention class Bugs
#1240/#1241 fixed for the temporal migration).

This module provides:

  - ``is_db_connectivity_error(exc)``: a pure classifier distinguishing
    transient DB-connectivity failures (PoolTimeout / connection drop) from
    genuine bugs (ProgrammingError, IntegrityError, ...), which must continue
    to log normally every time.
  - ``DbOutageThrottle``: a small stateful helper, one instance per
    loop/service, that collapses a connectivity-error storm into a single
    ERROR (transition into outage) + DEBUG follow-ups, a single recovery log
    on return to health, and an exponential-capped backoff for the caller's
    own retry interval.

Mirrors the classifier+throttle STYLE of
``code_indexer.services.provider_backoff`` (pure classifier function + small
stateful helper) but is intentionally independent of it — that module is
HTTP/429-specific and unrelated to PostgreSQL connectivity.

``psycopg`` is imported lazily inside ``is_db_connectivity_error`` so this
module can be imported from CLI-adjacent code paths without pulling psycopg
into startup (mirrors the lazy-import discipline already used in
``leader_election_service.py``).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

# Substrings recognized when an exception's type information has been lost
# (e.g. wrapped/stringified into a generic Exception) but the message still
# carries a recognizable PostgreSQL-connectivity phrase.
_CONNECTIVITY_MESSAGE_MARKERS = (
    "server closed the connection",
    "consuming input failed",
    "terminated abnormally",
    "couldn't get a connection",
    "PoolTimeout",
)


def is_db_connectivity_error(exc: BaseException) -> bool:
    """Return True iff ``exc`` represents a transient DB-connectivity failure.

    True when:
      - ``exc`` is an instance of ``psycopg.OperationalError`` (this also
        covers ``psycopg_pool.PoolTimeout``, which subclasses it, and the
        "server closed the connection unexpectedly" / "terminated
        abnormally" errors raised on a PG restart), OR
      - ``exc``'s message contains one of the known connectivity phrases
        (defense-in-depth for a signal that was wrapped/stringified and lost
        its original type).

    False for everything else, including genuine-bug exception classes such
    as ``psycopg.ProgrammingError`` / ``IntegrityError`` / ``DataError`` /
    ``InternalError`` / ``NotSupportedError`` — those must keep logging
    normally every time.

    Pure: no I/O, no logging, no side effects.
    """
    try:
        import psycopg  # lazy import — keeps startup fast
    except ImportError:  # pragma: no cover - psycopg is a hard dependency
        psycopg = None  # type: ignore[assignment]

    if psycopg is not None and isinstance(exc, psycopg.OperationalError):
        return True

    message = str(exc)
    return any(marker in message for marker in _CONNECTIVITY_MESSAGE_MARKERS)


class DbOutageThrottle:
    """Collapse a per-tick DB-connectivity error storm into single log events.

    One instance per background loop/service. Thread-safe via an internal
    lock (background loops run on daemon threads; an instance could in
    principle be touched from more than one thread).

    Usage::

        try:
            <db operation>
            throttle.on_db_success(logger)
        except Exception as exc:
            if not throttle.on_db_error(exc, logger):
                <ORIGINAL logging call, unchanged, for non-connectivity errors>

        wait_seconds = throttle.next_wait_seconds(normal_interval)
    """

    def __init__(self, service_name: str, max_backoff_seconds: float = 60.0) -> None:
        self._service_name = service_name
        self._max_backoff_seconds = max_backoff_seconds
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._outage_started_at: Optional[float] = None

    def on_db_error(self, exc: BaseException, logger: logging.Logger) -> bool:
        """Handle an exception raised by a DB operation.

        Returns False immediately (no state change, no logging) when ``exc``
        is not a connectivity error — the caller must then log it normally.

        Returns True when ``exc`` IS a connectivity error: the first error of
        a new outage logs exactly one ``logger.error(..., exc_info=True)``;
        every subsequent consecutive connectivity error logs only at DEBUG
        (no fresh traceback, no ERROR spam).
        """
        if not is_db_connectivity_error(exc):
            return False

        with self._lock:
            self._consecutive_failures += 1
            count = self._consecutive_failures
            if count == 1:
                self._outage_started_at = time.monotonic()

        if count == 1:
            logger.error(
                "%s: database unavailable, backing off (%s: %s)",
                self._service_name,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
        else:
            logger.debug(
                "%s: database still unavailable (consecutive failure #%d): %s: %s",
                self._service_name,
                count,
                type(exc).__name__,
                exc,
            )
        return True

    def on_db_success(self, logger: logging.Logger) -> None:
        """Record a successful DB operation.

        No-op (no log spam) if no outage was in progress. If an outage WAS in
        progress, logs exactly one recovery message and resets state so the
        next connectivity error starts a fresh outage cycle.
        """
        with self._lock:
            count = self._consecutive_failures
            started_at = self._outage_started_at
            self._consecutive_failures = 0
            self._outage_started_at = None

        if count == 0:
            return

        duration = time.monotonic() - started_at if started_at is not None else 0.0
        logger.warning(
            "%s: database recovered after %d consecutive failure(s) (%.1fs outage)",
            self._service_name,
            count,
            duration,
        )

    def next_wait_seconds(self, normal_interval: float) -> float:
        """Return the wait duration the caller's loop should sleep for.

        Returns ``normal_interval`` unchanged when no outage is in progress
        (zero behavior change on the happy path). When an outage is in
        progress, returns an exponentially growing value seeded from
        ``normal_interval``, capped at ``max_backoff_seconds``.
        """
        with self._lock:
            count = self._consecutive_failures

        if count <= 0:
            return normal_interval

        # Bug #1249 follow-up: clamp the EXPONENT, not just the final result.
        # 2 ** (count - 1) is computed as an arbitrary-precision int BEFORE the
        # min() below ever runs; for a long enough outage `count` grows
        # unbounded (it is never itself capped — only the returned wait value
        # is), and once that exponent crosses the IEEE-754 float ceiling,
        # converting the resulting bignum to float raises OverflowError from
        # OUTSIDE every caller's try/except (next_wait_seconds is always
        # called after the loop's own DB-exception handling), killing the
        # daemon thread this throttle exists to keep alive. Cap the exponent
        # at a value (30) far larger than any exponent that could ever
        # survive the max_backoff_seconds clamp below for any sane
        # interval/cap — this is purely an overflow guard and changes no
        # observable behavior on any realistic input.
        exponent = min(count - 1, 30)
        backoff = normal_interval * (2**exponent)
        return float(min(backoff, self._max_backoff_seconds))
