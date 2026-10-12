"""The control-plane metadata outbox sweeper (content seam step 5a).

Every alias / write-mode marker change made while ``metadata_file_dual_write``
is on commits an outbox row with the registry change. This sweeper -- one per
fleet, on the elected leader -- replays the outbox to the old files:

* **By key.** A row names a key, not a payload. For each claimed key the
  sweeper writes the registry's CURRENT document (or deletes the file when the
  key is gone) and drops every row for the key up to the one it served. A
  failed older row retried after a newer one therefore writes the newest
  value, never a stale one.
* **Only on success.** A row leaves the outbox only once its write succeeded.
  A failed write delays the file; it never drops it, and never fails the
  user-facing change.
* **Claimed.** Rows are claimed for the sweep (``FOR UPDATE SKIP LOCKED`` on
  PostgreSQL, a lease on SQLite), so two sweepers never write one key twice.
* **Drain bound.** With ``metadata_outbox_sink=local-files`` (the rollback
  sink) the clock starts on the leader's first such sweep; at
  ``metadata_outbox_drain_timeout_s`` the sweeper stops, logs every key still
  in the outbox by name, and does NOT keep retrying -- the rollback gate
  (depth 0) stays red.
* **Abandon.** :meth:`abandon` (``POST /api/admin/metadata-outbox/abandon``)
  logs the remaining keys by name, deletes exactly those rows and stops the
  sweeper; depth is then 0.

The stop state is in the registry (``metadata_outbox_state``), so an abandon
on any node stops the leader's sweeper. Changing ``metadata_outbox_sink``
clears it: the stop belongs to the sink it was reached with.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from code_indexer.global_repos.control_plane_metadata import (
    ControlPlaneMetadata,
    MetadataFiles,
)
from code_indexer.server.services.metadata_outbox.sinks import (
    MetadataSink,
    build_sink,
)
from code_indexer.server.storage.protocols.control_plane_metadata_backend import (
    ControlPlaneMetadataBackend,
    OutboxRow,
    OutboxState,
)
from code_indexer.server.utils.config_manager import (
    DEFAULT_METADATA_OUTBOX_DRAIN_TIMEOUT_S,
    METADATA_OUTBOX_SINK_LOCAL_FILES,
)

logger = logging.getLogger(__name__)

#: Seconds between sweeps on the leader.
DEFAULT_SWEEP_INTERVAL_SECONDS = 10.0
#: Rows claimed per sweep. Several rows for one key collapse to one write.
DEFAULT_SWEEP_BATCH_SIZE = 500

STOP_REASON_DRAIN_TIMEOUT = "drain_timeout"
STOP_REASON_ABANDONED = "abandoned"

#: Log message prefixes operators (and the tests) select on.
UNDRAINED_KEY_LOG = "metadata outbox: undrained key"
ABANDONED_KEY_LOG = "metadata outbox: abandoned key"

Key = Tuple[str, str]


@dataclass(frozen=True)
class OutboxSettings:
    """The sweeper's two runtime switches."""

    sink: str = METADATA_OUTBOX_SINK_LOCAL_FILES
    drain_timeout_s: int = DEFAULT_METADATA_OUTBOX_DRAIN_TIMEOUT_S


@dataclass
class SweepReport:
    """What one sweep did."""

    replayed: List[Key] = field(default_factory=list)
    failed: List[Key] = field(default_factory=list)
    stopped: Optional[str] = None
    undrained: List[Key] = field(default_factory=list)


def _row_key(row: OutboxRow) -> Key:
    return (row.kind, row.name)


def _unique_keys(rows: List[OutboxRow]) -> List[Key]:
    return list(dict.fromkeys(_row_key(row) for row in rows))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MetadataOutboxSweeper:
    """Replays the outbox to the files; runs on the elected leader only."""

    def __init__(
        self,
        *,
        metadata: ControlPlaneMetadata,
        backend: ControlPlaneMetadataBackend,
        settings: Callable[[], OutboxSettings],
        sink_factory: Callable[[str], MetadataSink] = build_sink,
        leader_check: Callable[[], bool] = lambda: True,
        now_fn: Callable[[], float] = time.time,
        interval_s: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
        batch_size: int = DEFAULT_SWEEP_BATCH_SIZE,
    ) -> None:
        self._metadata = metadata
        self._backend = backend
        self._settings = settings
        self._sink_factory = sink_factory
        self._leader_check = leader_check
        self._now_fn = now_fn
        self._interval_s = interval_s
        self._batch_size = batch_size
        self._sinks: Dict[str, MetadataSink] = {}
        self._sink_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Sweeping
    # ------------------------------------------------------------------

    def _sink(self, name: str) -> MetadataSink:
        with self._sink_lock:
            if name not in self._sinks:
                self._sinks[name] = self._sink_factory(name)
            return self._sinks[name]

    def depth(self) -> int:
        return self._backend.outbox_depth()

    def sweep_once(self) -> SweepReport:
        """One sweep: honour the stop state and the drain bound, then replay
        up to one batch of claimed keys."""
        settings = self._settings()
        state = self._backend.get_outbox_state()

        if state.stopped_at is not None:
            if state.stopped_sink == settings.sink:
                return SweepReport(stopped=state.stopped_reason)
            logger.info(
                "metadata outbox: sink changed %s -> %s; sweeper resumes",
                state.stopped_sink,
                settings.sink,
            )
            state = OutboxState()
            self._backend.save_outbox_state(state)

        bound_report = self._apply_drain_bound(settings, state)
        if bound_report is not None:
            return bound_report

        return self._replay(self._sink(settings.sink))

    def _apply_drain_bound(
        self, settings: OutboxSettings, state: OutboxState
    ) -> Optional[SweepReport]:
        """Start, keep, or end the local-files drain clock. Returns a report
        when the sweeper stopped at the bound."""
        if settings.sink != METADATA_OUTBOX_SINK_LOCAL_FILES:
            if state.drain_started_epoch is not None:
                state.drain_started_epoch = None
                state.drain_sink = None
                self._backend.save_outbox_state(state)
            return None

        now = self._now_fn()
        if state.drain_started_epoch is None or state.drain_sink != settings.sink:
            state.drain_started_epoch = now
            state.drain_sink = settings.sink
            self._backend.save_outbox_state(state)
            logger.info(
                "metadata outbox: drain to local files started (bound %ss)",
                settings.drain_timeout_s,
            )
            return None

        if now - state.drain_started_epoch < settings.drain_timeout_s:
            return None

        undrained = _unique_keys(self._backend.outbox_rows())
        if not undrained:
            return None  # fully drained: nothing to stop, nothing to log
        for kind, name in undrained:
            logger.error(
                "%s %s %r (drain bound %ss reached; the sweeper stops)",
                UNDRAINED_KEY_LOG,
                kind,
                name,
                settings.drain_timeout_s,
            )
        state.stopped_at = _now_iso()
        state.stopped_reason = STOP_REASON_DRAIN_TIMEOUT
        state.stopped_sink = settings.sink
        self._backend.save_outbox_state(state)
        return SweepReport(stopped=STOP_REASON_DRAIN_TIMEOUT, undrained=undrained)

    def _replay(self, sink: MetadataSink) -> SweepReport:
        report = SweepReport()
        files = self._metadata.files
        with self._backend.claim_outbox(self._batch_size) as claim:
            for kind, name, up_to_id in claim.keys():
                try:
                    document = claim.current(kind, name)
                    path = str(files.path_for(kind, name))
                    if document is None:
                        sink.delete(path)
                    else:
                        sink.put(path, MetadataFiles.encode(document))
                    claim.complete(kind, name, up_to_id)
                except Exception as exc:
                    report.failed.append((kind, name))
                    logger.warning(
                        "metadata outbox: replay of %s %r through %s failed "
                        "(row kept, retried next sweep): %s",
                        kind,
                        name,
                        sink.name,
                        exc,
                    )
                    continue
                report.replayed.append((kind, name))
        return report

    # ------------------------------------------------------------------
    # Operator override
    # ------------------------------------------------------------------

    def abandon(self, requested_by: str) -> List[Key]:
        """Log the remaining keys by name, delete exactly those rows, and stop
        the sweeper. Returns the abandoned keys."""
        rows = self._backend.outbox_rows()
        keys = _unique_keys(rows)
        for kind, name in keys:
            logger.warning(
                "%s %s %r (abandoned by %s; re-apply by hand if a rolled-back "
                "server needs it)",
                ABANDONED_KEY_LOG,
                kind,
                name,
                requested_by,
            )
        deleted = self._backend.delete_outbox_rows([row.id for row in rows])
        state = self._backend.get_outbox_state()
        state.stopped_at = _now_iso()
        state.stopped_reason = STOP_REASON_ABANDONED
        state.stopped_sink = self._settings().sink
        self._backend.save_outbox_state(state)
        logger.warning(
            "metadata outbox: %d row(s) for %d key(s) abandoned by %s; sweeper stopped",
            deleted,
            len(keys),
            requested_by,
        )
        return keys

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="MetadataOutboxSweeper", daemon=True
        )
        self._thread.start()
        logger.info("MetadataOutboxSweeper started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("MetadataOutboxSweeper stopped")

    def _loop(self) -> None:
        while not self._stop_event.wait(self._interval_s):
            try:
                if self._leader_check():
                    self.sweep_once()
            except Exception:
                logger.exception("metadata outbox: sweep failed; retrying next cycle")
