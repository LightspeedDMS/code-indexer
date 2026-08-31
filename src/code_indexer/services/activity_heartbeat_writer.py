"""ActivityHeartbeatWriter: child-side periodic heartbeat file writer.

Issue #1530, Priority 2 (child-side half). A `cidx index --progress-json`
subprocess spawned by the server uses this writer to periodically serialize
its process-wide `ActivityBeacon`'s in-flight state to a file the PARENT
(the node that called Popen) can read to detect a genuine wedge -- see
`code_indexer.services.activity_watchdog` for the parent-side reader/
staleness-evaluation half of this mechanism.

Design constraints from the issue, both load-bearing:

1. The heartbeat file must exist with a BOOTSTRAP record immediately, before
   any real indexing work begins -- the original production hang was
   observed at "semantic: starting...", before a single tick could have
   fired. `start()` performs this write synchronously before returning.
2. The write is atomic (temp file + `os.replace`) so a concurrent parent-
   side reader never observes a torn/partial write. No fsync is used here
   (unlike `HNSWIndexManager._atomic_write_metadata_durable`) -- this is
   disposable heartbeat data with a short lifetime, not data that must
   survive a crash; only the read-never-sees-garbage property matters.

Single-writer, single-reader by construction (Issue #1530 design point 5):
one file per subprocess invocation, generated fresh by the parent before
spawn -- an internal, server-generated path (under the node's own temp/data
dir, never derived from untrusted request input), never a path supplied by
an HTTP client. `heartbeat_path` MUST be an absolute string: this is a
cheap, mandatory sanity check (a relative path would resolve against
whatever the child's cwd happens to be, defeating the "node-local,
well-known location" design point), not a defense against adversarial
input from an external caller. No file locking of any kind is needed.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
from typing import Optional

from code_indexer.services.activity_beacon import ActivityBeacon

logger = logging.getLogger(__name__)

#: Env var name the parent sets (merged into the child's env, never a
#: standalone env= dict per this project's child-wiring convention) to tell
#: the child where to write its heartbeat file. Absence means "no watchdog
#: requested" -- the child-side CLI wiring (a later priority) must treat a
#: missing env var as fully opt-in/no-op, never as an error.
ACTIVITY_HEARTBEAT_PATH_ENV = "CIDX_ACTIVITY_HEARTBEAT_PATH"

#: How often the background thread re-writes the snapshot after the initial
#: bootstrap write. Deliberately small relative to any sane staleness
#: threshold (the issue proposes 90-120s) so the parent always has a recent
#: sample well before a real threshold trip.
DEFAULT_HEARTBEAT_WRITE_INTERVAL_SECONDS = 2.0

#: Extra grace period added on top of one write interval when joining the
#: background thread in stop() -- generous enough to absorb one full write
#: cycle plus scheduling jitter, without blocking indefinitely.
_STOP_JOIN_GRACE_SECONDS = 1.0


def _validate_heartbeat_path(heartbeat_path: object) -> str:
    if not isinstance(heartbeat_path, str) or not os.path.isabs(heartbeat_path):
        raise ValueError(
            f"heartbeat_path must be an absolute str path, got {heartbeat_path!r}"
        )
    return heartbeat_path


def _validate_write_interval_seconds(write_interval_seconds: object) -> float:
    if (
        isinstance(write_interval_seconds, bool)
        or not isinstance(write_interval_seconds, (int, float))
        or math.isnan(write_interval_seconds)
        or math.isinf(write_interval_seconds)
        or write_interval_seconds <= 0
    ):
        raise ValueError(
            f"write_interval_seconds must be a finite number > 0, "
            f"got {write_interval_seconds!r}"
        )
    return float(write_interval_seconds)


def _build_heartbeat_payload(beacon: ActivityBeacon, bootstrap: bool) -> dict:
    payload = {"pid": os.getpid(), "bootstrap": bootstrap}
    payload.update(beacon.snapshot())
    return payload


def _atomic_write_json_file(target_path: str, payload: dict) -> None:
    """Write `payload` as JSON to `target_path` atomically (temp file in the
    same directory + `os.replace`) so a concurrent reader never observes a
    torn/partial write. No fsync: disposable heartbeat data, short lifetime.
    """
    directory = os.path.dirname(target_path) or "."
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=".activity_heartbeat_", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w") as tmp_f:
            json.dump(payload, tmp_f)
        os.replace(tmp_path, target_path)
    except Exception:
        logger.warning(
            "ActivityHeartbeatWriter: failed to write heartbeat file %s",
            target_path,
            exc_info=True,
        )
        try:
            os.unlink(tmp_path)
        except OSError as cleanup_err:
            logger.warning(
                "ActivityHeartbeatWriter: failed to clean up temp file %s "
                "after a write error: %s",
                tmp_path,
                cleanup_err,
            )
        raise


def _run_periodic_writer(
    beacon: ActivityBeacon,
    heartbeat_path: str,
    write_interval_seconds: float,
    stop_event: threading.Event,
) -> None:
    """Background-thread target: re-write the snapshot every interval until
    `stop_event` is set. The bootstrap write happens synchronously in
    `ActivityHeartbeatWriter.start()`, before this loop is ever started.

    A failed cycle must never end this loop. Letting an exception propagate
    kills the thread outright -- the heartbeat file then simply stops being
    updated, and the PARENT watchdog reads a frozen file as a wedged
    process. One transient disk/permission hiccup must not get a healthy,
    possibly multi-hour indexing job killed; retrying on the next interval
    is both harmless and self-healing. The failure is logged here (WARNING,
    with traceback) rather than relying on the write helper's own logging,
    because payload construction is not self-logging. A genuinely
    persistent failure still surfaces: every cycle logs, and the file stays
    stale, so the watchdog's mtime signal works exactly as designed.
    """
    while not stop_event.wait(timeout=write_interval_seconds):
        try:
            payload = _build_heartbeat_payload(beacon, bootstrap=False)
            _atomic_write_json_file(heartbeat_path, payload)
        except Exception:  # noqa: BLE001 - see docstring
            logger.warning(
                "ActivityHeartbeatWriter: heartbeat cycle failed for %s; "
                "retrying on the next interval",
                heartbeat_path,
                exc_info=True,
            )


class ActivityHeartbeatWriter:
    """Periodically persists `beacon.snapshot()` to `heartbeat_path`.

    Usage (child process)::

        writer = ActivityHeartbeatWriter(beacon=get_activity_beacon(), heartbeat_path=path)
        writer.start()   # bootstrap record written before this returns
        ... run real indexing work, using beacon.tick(...) throughout ...
        writer.stop()    # stops the background thread; does NOT delete the
                          # file -- the PARENT owns file cleanup (issue
                          # design point 5: "the watching node deletes the
                          # file when the child exits").
    """

    def __init__(
        self,
        beacon: ActivityBeacon,
        heartbeat_path: str,
        write_interval_seconds: float = DEFAULT_HEARTBEAT_WRITE_INTERVAL_SECONDS,
    ) -> None:
        if beacon is None:
            raise ValueError("beacon must not be None")
        self._beacon = beacon
        self._heartbeat_path = _validate_heartbeat_path(heartbeat_path)
        self._write_interval_seconds = _validate_write_interval_seconds(
            write_interval_seconds
        )
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Write the bootstrap record synchronously, then start the
        periodic background writer thread.
        """
        _atomic_write_json_file(
            self._heartbeat_path,
            _build_heartbeat_payload(self._beacon, bootstrap=True),
        )
        self._thread = threading.Thread(
            target=_run_periodic_writer,
            args=(
                self._beacon,
                self._heartbeat_path,
                self._write_interval_seconds,
                self._stop_event,
            ),
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the background thread to stop and wait (bounded) for it
        to actually terminate. Does not delete the heartbeat file (parent-
        side cleanup responsibility, per the issue's design).

        Logs a WARNING (never raises) if the thread is still alive after
        the bounded join -- this is a daemon thread, so it can never
        actually block process exit, but a caller relying on "stop()
        guarantees the thread is gone" deserves a visible signal if that
        didn't happen (e.g. it was itself wedged inside the write).
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(
                timeout=self._write_interval_seconds + _STOP_JOIN_GRACE_SECONDS
            )
            if self._thread.is_alive():
                logger.warning(
                    "ActivityHeartbeatWriter: background writer thread for "
                    "%s did not terminate within the expected join timeout",
                    self._heartbeat_path,
                )
