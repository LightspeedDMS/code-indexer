"""
Backfill Journal Service — Story #1062.

Manages per-namespace (lifecycle / description) shared-NFS activity journals and
_status.json sidecars for the lifecycle backfill and description-refresh backfill.

Each namespace has:
  {journal_dir}/_activity.md  — append-only markdown journal (ActivityJournalService)
  {journal_dir}/_status.json  — atomic sidecar (tempfile + os.replace) tracking
                                 running, started_at, completed_at, total, done, failed

Cross-node reads use ActivityJournalService.get_content_from_path() and direct
sidecar reads (no locking beyond the in-process threading.Lock for journal appends).

Failure contract: if journal dir cannot be initialised (NFS gone, permission error),
start() logs and swallows — never fatal to the backfill.

Restart semantics: start() truncates the journal for a fresh session. A server restart
mid-backfill resets the journal on the next run (matches dep-map behaviour).
"""

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, cast

from code_indexer.server.services.activity_journal_service import ActivityJournalService

logger = logging.getLogger(__name__)

# Server-side post-completion grace: keep X-Backfill-Active=1 for this many seconds
# after completed_at, so the card stays visible across a page reload.
BACKFILL_GRACE_SECONDS: int = 30

# Namespace labels used in journal entries
_NAMESPACE_LABELS: Dict[str, str] = {
    "lifecycle": "Lifecycle",
    "description": "Description",
}

_STATUS_FILENAME = "_status.json"


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """Write *data* as JSON to *path* atomically via tempfile + os.replace.

    NFSv4-safe: the temp file is created in the same directory so os.replace
    is a same-filesystem rename. Cleans up tmp on failure.
    """
    tmp_name = str(path) + f".tmp.{uuid.uuid4().hex}"
    try:
        with open(tmp_name, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class BackfillJournalService:
    """
    Per-namespace backfill journal + sidecar manager.

    Wraps ActivityJournalService for the markdown journal and maintains a
    _status.json sidecar for cluster-shared status (running, counters, timestamps).

    Thread-safe for concurrent calls from within one process. Cross-node
    reads go directly to the filesystem (ActivityJournalService.get_content_from_path
    + sidecar read).

    Args:
        namespace: "lifecycle" or "description" — controls entry labels.
        journal_dir: Directory for _activity.md and _status.json.
                     Created by start() if absent (degraded silently on failure).
    """

    def __init__(self, namespace: str, journal_dir: Path) -> None:
        self._namespace = namespace
        self._journal_dir = Path(journal_dir)
        self._label = _NAMESPACE_LABELS.get(namespace, namespace.capitalize())
        self._journal = ActivityJournalService()
        self._lock = threading.Lock()
        # Counters — maintained in-process, mirrored to sidecar on each mutation
        self._total: int = 0
        self._done: int = 0
        self._failed: int = 0
        self._started = False  # True once start() succeeds
        self._completed = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, total: int) -> None:
        """
        Begin a new backfill session.

        Creates (or truncates) the journal and writes an initial _status.json.
        If the journal dir cannot be initialised (NFS gone, permission error),
        logs and swallows — never fatal.

        Args:
            total: Total number of aliases to process.
        """
        with self._lock:
            self._total = total
            self._done = 0
            self._failed = 0
            self._started = False
            self._completed = False

        try:
            self._journal_dir.mkdir(parents=True, exist_ok=True)
            self._journal.init(self._journal_dir)
            with self._lock:
                self._started = True
        except Exception as exc:
            logger.warning(
                "BackfillJournalService[%s]: journal init failed (degraded observability): %s",
                self._namespace,
                exc,
            )
            # Continue with degraded observability — backfill must not abort

        # Write initial sidecar and journal entry even if journal init failed
        try:
            now = datetime.now(timezone.utc)
            self._write_sidecar(
                running=True,
                started_at=now,
                completed_at=None,
            )
        except Exception as exc:
            logger.warning(
                "BackfillJournalService[%s]: sidecar write failed: %s",
                self._namespace,
                exc,
            )

        self._journal.log(
            f"{self._label} backfill: started — {total} broken aliases",
            source="backfill",
        )

    def update_alias(
        self, alias: str, success: bool, reason: Optional[str] = None
    ) -> None:
        """
        Record the outcome of processing one alias.

        Updates done/failed counters, writes sidecar, appends journal entry.
        No-op if start() was never called (degraded mode).

        Args:
            alias: Repository alias that was processed.
            success: True if processing succeeded or was skipped, False on failure.
            reason: Optional reason string (included in journal entry on failure or skip).
        """
        with self._lock:
            if not self._started and not self._completed:
                # Not initialised — silently skip (degraded mode)
                pass
            if success:
                self._done += 1
            else:
                self._failed += 1

        if success:
            msg = f"alias {alias}: succeeded"
        else:
            reason_str = f": {reason}" if reason else ""
            msg = f"alias {alias}: failed{reason_str}"

        self._journal.log(msg, source="backfill")

        try:
            self._write_sidecar(running=True)
        except Exception as exc:
            logger.debug(
                "BackfillJournalService[%s]: sidecar update failed (non-fatal): %s",
                self._namespace,
                exc,
            )

    def complete(self) -> None:
        """
        Mark the backfill as complete (terminal state).

        Writes sidecar with running=False and completed_at=now.
        Appends the terminal summary journal entry.
        No-op if start() was never called.
        """
        with self._lock:
            if not self._started and not self._completed:
                return
            done = self._done
            failed = self._failed
            self._completed = True

        # _process_one_repo only succeeds or raises — no skip path exists.
        parts = [f"{done} succeeded", f"{failed} failed"]

        self._journal.log(
            f"{self._label} backfill complete: {', '.join(parts)}",
            source="backfill",
        )

        try:
            now = datetime.now(timezone.utc)
            self._write_sidecar(running=False, completed_at=now)
        except Exception as exc:
            logger.debug(
                "BackfillJournalService[%s]: terminal sidecar write failed (non-fatal): %s",
                self._namespace,
                exc,
            )

    def is_active(self) -> bool:
        """
        Return True while running OR within BACKFILL_GRACE_SECONDS of completion.

        Reads from the on-disk sidecar so cross-node reads work correctly.
        Returns False if no sidecar exists (never started).

        Note: a hard-crash (SIGKILL) mid-backfill leaves running=True with no
        stale-timeout, so the card stays visible until the next backfill's start()
        overwrites the sidecar (self-heals next run) — acceptable for observability.
        """
        status = self.get_status()
        if status is None:
            return False
        if status.get("running"):
            return True
        completed_at_str = status.get("completed_at")
        if not completed_at_str:
            return False
        try:
            completed_at = datetime.fromisoformat(completed_at_str)
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - completed_at).total_seconds()
            return age < BACKFILL_GRACE_SECONDS
        except (ValueError, TypeError):
            return False

    def get_status(self) -> Optional[Dict[str, Any]]:
        """
        Read and return the _status.json sidecar as a dict, or None if absent/corrupt.

        Always reads from disk so any cluster node sees the authoritative state.
        """
        sidecar = self._journal_dir / _STATUS_FILENAME
        try:
            raw = sidecar.read_text(encoding="utf-8")
            return cast(Dict[str, Any], json.loads(raw))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            logger.warning(
                "BackfillJournalService[%s]: corrupt _status.json: %s",
                self._namespace,
                exc,
            )
            return None

    @property
    def journal_path(self) -> Optional[Path]:
        """Return the activity journal file path (for cross-node reads)."""
        p = self._journal_dir / "_activity.md"
        return p if p.exists() else None

    @property
    def journal_dir(self) -> Path:
        """Return the journal directory."""
        return self._journal_dir

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _write_sidecar(
        self,
        running: bool,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
    ) -> None:
        """Atomically write _status.json with current counters."""
        sidecar = self._journal_dir / _STATUS_FILENAME

        # Read existing sidecar to preserve started_at if not provided
        existing = self.get_status() or {}
        if started_at is None:
            started_at_str = existing.get("started_at")
        else:
            started_at_str = started_at.isoformat()

        if completed_at is not None:
            completed_at_str: Optional[str] = completed_at.isoformat()
        else:
            completed_at_str = existing.get("completed_at")
            # When running=True, always clear completed_at
            if running:
                completed_at_str = None

        with self._lock:
            data: Dict[str, Any] = {
                "running": running,
                "started_at": started_at_str,
                "completed_at": completed_at_str,
                "total": self._total,
                "done": self._done,
                "failed": self._failed,
            }

        _atomic_write_json(sidecar, data)
