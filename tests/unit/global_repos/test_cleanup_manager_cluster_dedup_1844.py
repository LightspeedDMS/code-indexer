"""Bug #1844 -- CleanupManager cannot delete CoW snapshots (cluster disk leak).

Four defects, each proven here by a test that FAILS against unmodified code.

1. CROSS-NODE DUPLICATION (the discriminating two-process test).
   ``GoldenRepoMetadataBackend.list_cleanup_pending_deletions()`` is
   ``SELECT index_path, scheduled_at FROM cleanup_pending_deletion_state``
   with NO node predicate (both the SQLite and the PostgreSQL
   implementations). Every node -- and every ``uvicorn --workers N``
   worker -- therefore hydrates the WHOLE fleet queue and independently
   attempts every delete, while ``_failure_counts``/``_next_retry_times``
   live in per-process RAM. That is per-node RAM governing a decision
   whose correctness depends on other nodes, and it is why one stuck
   snapshot produces an ERROR burst on several hosts at once. The fix
   claims the path through the established cluster-atomic arbiter
   (``JobTracker.register_job_if_no_conflict``, backed by the
   ``idx_active_job_per_repo`` partial unique index) so exactly one node
   issues the DELETE.

   A single-process test cannot fail for the right reason here: the
   defect IS two processes racing one shared durable queue, so the test
   runs two real OS processes against one real SQLite database.

2. "STILL IN USE" IS TREATED AS A HARD FAILURE.
   A delete the daemon refuses because the clone is still held is not an
   error, it is "not yet". Today every failure is identical, so the
   5-attempt/~16-second circuit breaker is burned on a transient
   condition and the path is abandoned -- the disk is never reclaimed.

3. THE DAEMON'S REAL CAUSE NEVER CROSSES THE WIRE.
   ``resp.raise_for_status()`` yields an HTTPError carrying only the
   status line and the URL. The daemon's errno exists only in the
   daemon host's own log, which is why this bug's root cause could not
   be established from the CIDX side at all.

4. SILENT FAILURE ON THE LOCAL BACKEND.
   ``LocalCloneBackend.delete_clone`` returns ``False`` on OSError and
   ``CleanupManager._delete_index`` discards the return value, so a
   failed deletion is recorded as a success: the path leaves the queue
   AND the durable row is deleted. That is a permanent, silent leak
   (Messi Rule #13) -- the solo-topology mirror of this same bug.
"""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing.synchronize import Barrier
from pathlib import Path
from typing import Dict, Iterator, List

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.shared.clone_backend import (
    CowDaemonBackend,
    LocalCloneBackend,
)
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.server.storage.sqlite_backends import (
    GoldenRepoMetadataSqliteBackend,
)
from code_indexer.server.utils.config_manager import CowDaemonConfig


CLEANUP_LOGGER = "code_indexer.global_repos.cleanup_manager"

#: Shape of the scripted daemon's JSON error body: a FLAT object of string
#: fields. The daemon's own ``http_exception_handler`` returns
#: ``exc.detail`` as the entire body whenever it is a dict, so a real
#: 409/500 from the CoW daemon arrives flat, not nested under "detail".
DaemonErrorPayload = Dict[str, str]

#: How long the winning "node" holds the cluster claim while deleting, so the
#: losing node's claim attempt provably overlaps it. Bounded and short.
CLAIM_HOLD_SECONDS = 1.0

#: Bounded join/barrier deadline for the two-process test.
PROCESS_DEADLINE_SECONDS = 90.0


# ---------------------------------------------------------------------------
# Two-process (two-node) support -- module level so `spawn` can import it
# ---------------------------------------------------------------------------


class AuditingSnapshotManager:
    """A REAL :class:`VersionedSnapshotManager` over a REAL
    :class:`LocalCloneBackend`, which additionally appends one line per
    delete ATTEMPT to a shared on-disk audit file.

    Nothing about the deletion itself is faked -- the inner manager really
    removes the directory. The audit file exists only because the two
    attempts happen in two different OS processes, so an in-memory counter
    could not observe both.
    """

    def __init__(self, versioned_base: str, audit_path: str) -> None:
        self._inner = VersionedSnapshotManager(
            versioned_base=versioned_base,
            clone_backend=LocalCloneBackend(versioned_base=versioned_base),
        )
        self._audit_path = audit_path

    def is_versioned_snapshot(self, path: str) -> bool:
        return bool(self._inner.is_versioned_snapshot(path))

    def delete_snapshot(self, alias: str, version_path: str) -> bool:
        # O_APPEND write of a short line is atomic across processes.
        with open(self._audit_path, "a", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()} {version_path}\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Hold the claim long enough that the peer node's claim attempt
        # provably overlaps this one.
        time.sleep(CLAIM_HOLD_SECONDS)
        return bool(self._inner.delete_snapshot(alias, version_path))


def cluster_node_worker(
    db_path: str,
    versioned_base: str,
    snapshot_path: str,
    audit_path: str,
    barrier: Barrier,
) -> None:
    """One simulated cluster node: its own process, its own CleanupManager,
    its own JobTracker -- sharing ONE SQLite database with its peer."""
    from code_indexer.server.services.job_tracker import JobTracker

    backend = GoldenRepoMetadataSqliteBackend(db_path)
    manager = CleanupManager(
        query_tracker=QueryTracker(),
        job_tracker=JobTracker(db_path),
        min_retention_age_seconds=0.0,
        persistence_backend=backend,
    )
    manager.set_snapshot_manager(AuditingSnapshotManager(versioned_base, audit_path))

    # Both nodes must have hydrated the shared durable row BEFORE either
    # starts processing -- otherwise the winner could remove the row before
    # the loser ever sees it and the race would not be exercised at all.
    assert snapshot_path in manager.get_pending_cleanups()

    barrier.wait(timeout=PROCESS_DEADLINE_SECONDS)
    manager._process_cleanup_queue()


# ---------------------------------------------------------------------------
# A real (tiny) HTTP daemon stand-in -- real sockets, real requests round trip
# ---------------------------------------------------------------------------


class _ScriptedDaemonHandler(BaseHTTPRequestHandler):
    """Answers DELETE /api/v1/clones/{ns}/{name} with a scripted response."""

    status_code: int = 500
    payload: DaemonErrorPayload = {}

    def do_DELETE(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler API
        body = json.dumps(type(self).payload).encode()
        self.send_response(type(self).status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Silence the stderr access log this handler would otherwise emit."""
        return


@dataclass
class CycleOutcome:
    """What one cleanup cycle against a scripted daemon produced."""

    manager: CleanupManager
    records: List[logging.LogRecord] = field(default_factory=list)

    @property
    def error_messages(self) -> List[str]:
        return [
            record.getMessage()
            for record in self.records
            if record.levelno >= logging.ERROR and record.name == CLEANUP_LOGGER
        ]

    @property
    def all_messages(self) -> str:
        return "\n".join(record.getMessage() for record in self.records)


@contextmanager
def _run_one_cleanup_cycle_against_daemon(
    caplog: pytest.LogCaptureFixture,
    mount_point: str,
    snapshot_path: str,
    status_code: int,
    payload: DaemonErrorPayload,
) -> Iterator[CycleOutcome]:
    """Drive exactly one ``_process_cleanup_queue`` cycle of a real
    CleanupManager -> real VersionedSnapshotManager -> real CowDaemonBackend
    -> real HTTP round trip against a daemon scripted to answer
    ``status_code``/``payload``, then yield the outcome for assertions."""
    handler = type(
        "_Handler",
        (_ScriptedDaemonHandler,),
        {"status_code": status_code, "payload": payload},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    daemon_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        backend = CowDaemonBackend(
            config=CowDaemonConfig(
                daemon_url=daemon_url,
                api_key="test-key",
                mount_point=mount_point,
                poll_interval_seconds=1,
                timeout_seconds=5,
                daemon_storage_path=mount_point,
                request_timeout_seconds=5,
            ),
            visibility_waiter=lambda _path: None,
        )
        manager = CleanupManager(
            query_tracker=QueryTracker(), min_retention_age_seconds=0.0
        )
        manager.set_snapshot_manager(
            VersionedSnapshotManager(versioned_base=mount_point, clone_backend=backend)
        )
        manager.schedule_cleanup(snapshot_path)
        with caplog.at_level(logging.DEBUG, logger=CLEANUP_LOGGER):
            manager._process_cleanup_queue()
        yield CycleOutcome(manager=manager, records=list(caplog.records))
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mount_point(tmp_path: Path) -> str:
    mount = tmp_path / "cow-storage"
    mount.mkdir()
    return str(mount)


@pytest.fixture
def snapshot_path(mount_point: str) -> str:
    snapshot = Path(mount_point) / ".versioned" / "repo_ns" / "v_1789090800"
    snapshot.mkdir(parents=True)
    (snapshot / "hnsw_index.bin").write_bytes(b"x" * 64)
    return str(snapshot)


# ---------------------------------------------------------------------------
# Defect 1 -- cross-node duplication (TWO REAL PROCESSES)
# ---------------------------------------------------------------------------


class TestClusterWideDeleteIsClaimedExactlyOnce:
    def test_two_nodes_sharing_the_durable_queue_issue_exactly_one_delete(
        self, tmp_path: Path, mount_point: str, snapshot_path: str
    ) -> None:
        """Two independent processes, one shared pending-deletion queue.

        RED (unmodified code): both nodes hydrate the same durable row and
        both call delete_snapshot -- two DELETEs against one daemon clone,
        two ERROR lines per failing snapshot per node.
        GREEN: exactly one node wins the cluster-atomic claim.
        """
        db_path = str(tmp_path / "server.db")
        DatabaseSchema(db_path).initialize_database()
        backend = GoldenRepoMetadataSqliteBackend(db_path)
        backend.ensure_table_exists()
        # Scheduled long ago so the minimum-retention-age floor is satisfied.
        backend.schedule_cleanup_deletion(snapshot_path, time.time() - 10_000.0)

        audit_path = str(tmp_path / "delete_attempts.log")
        Path(audit_path).touch()

        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(2)
        procs = [
            ctx.Process(
                target=cluster_node_worker,
                args=(db_path, mount_point, snapshot_path, audit_path, barrier),
            )
            for _ in range(2)
        ]
        for proc in procs:
            proc.start()
        try:
            for proc in procs:
                proc.join(timeout=PROCESS_DEADLINE_SECONDS)
        finally:
            for proc in procs:
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=10)

        for proc in procs:
            assert proc.exitcode == 0, f"node process failed: exitcode={proc.exitcode}"

        attempts = [
            line
            for line in Path(audit_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(attempts) == 1, (
            "expected exactly ONE cluster-wide delete attempt for "
            f"{snapshot_path}, got {len(attempts)}: {attempts}"
        )
        assert not Path(snapshot_path).exists()


# ---------------------------------------------------------------------------
# Defects 2 and 3 -- daemon response classification
# ---------------------------------------------------------------------------


IN_USE_PAYLOAD: DaemonErrorPayload = {
    "error": "Clone directory is still in use",
    "code": "CLONE_IN_USE",
    "errno": "ENOTEMPTY",
    "detail": "[Errno 39] Directory not empty: '/srv/cow/v_1'",
}

BROKEN_PAYLOAD: DaemonErrorPayload = {
    "error": "Failed to remove clone directory",
    "code": "CLONE_DELETE_FAILED",
    "errno": "EACCES",
    "detail": "[Errno 13] Permission denied: '/srv/cow/v_1/x'",
}


class TestDaemonDeleteFailureClassification:
    def test_still_in_use_does_not_burn_the_failure_budget_or_log_error(
        self, caplog: pytest.LogCaptureFixture, mount_point: str, snapshot_path: str
    ) -> None:
        """A 409 'clone still in use' is 'not yet', not an error.

        RED: the generic HTTPError is counted as a hard failure (burning
        one of the five circuit-breaker attempts) and logged at ERROR.
        GREEN: zero failures counted, zero ERROR records, path stays queued.
        """
        with _run_one_cleanup_cycle_against_daemon(
            caplog, mount_point, snapshot_path, 409, IN_USE_PAYLOAD
        ) as outcome:
            assert outcome.error_messages == [], (
                "a still-in-use snapshot must not be logged as an ERROR; got: "
                f"{outcome.error_messages}"
            )
            assert outcome.manager._get_failure_count(snapshot_path) == 0, (
                "a still-in-use snapshot must not consume the circuit-breaker "
                "budget -- it is a retryable 'not yet', not a failure"
            )
            assert snapshot_path in outcome.manager.get_pending_cleanups()

    def test_non_retryable_failure_still_counts_and_still_logs_error(
        self, caplog: pytest.LogCaptureFixture, mount_point: str, snapshot_path: str
    ) -> None:
        """The existing loud-failure contract must be preserved for genuinely
        broken deletes (this one passes today and must keep passing)."""
        with _run_one_cleanup_cycle_against_daemon(
            caplog, mount_point, snapshot_path, 500, BROKEN_PAYLOAD
        ) as outcome:
            assert outcome.manager._get_failure_count(snapshot_path) == 1
            assert outcome.error_messages != []

    def test_daemon_errno_is_present_in_the_cleanup_error_message(
        self, caplog: pytest.LogCaptureFixture, mount_point: str, snapshot_path: str
    ) -> None:
        """RED: the logged message is only
        '500 Server Error: Internal Server Error for url: ...' -- the daemon's
        errno never crosses the wire, which is exactly why this bug's root
        cause could not be established from the CIDX side."""
        with _run_one_cleanup_cycle_against_daemon(
            caplog, mount_point, snapshot_path, 500, BROKEN_PAYLOAD
        ) as outcome:
            assert "EACCES" in outcome.all_messages, (
                "the daemon's own errno must be visible in this side's ERROR "
                f"log; got:\n{outcome.all_messages}"
            )


# ---------------------------------------------------------------------------
# Defect 4 -- a falsey delete result must never be read as success
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root bypasses directory permissions; EACCES unreachable"
)
class TestFailedLocalDeleteIsNotSilentlyTreatedAsSuccess:
    def test_undeletable_snapshot_stays_queued_and_durably_pending(
        self, tmp_path: Path, mount_point: str, snapshot_path: str
    ) -> None:
        """RED: LocalCloneBackend.delete_clone returns False on OSError and
        _delete_index discards the result, so the path is dropped from the
        queue AND its durable row is deleted -- a permanent silent leak."""
        db_path = str(tmp_path / "server.db")
        DatabaseSchema(db_path).initialize_database()
        backend = GoldenRepoMetadataSqliteBackend(db_path)
        backend.ensure_table_exists()

        # Real, deterministic rmtree failure: the snapshot directory is not
        # writable, so unlinking its contents raises EACCES.
        os.chmod(snapshot_path, 0o500)
        try:
            manager = CleanupManager(
                query_tracker=QueryTracker(),
                min_retention_age_seconds=0.0,
                persistence_backend=backend,
            )
            manager.set_snapshot_manager(
                VersionedSnapshotManager(
                    versioned_base=mount_point,
                    clone_backend=LocalCloneBackend(versioned_base=mount_point),
                )
            )
            manager.schedule_cleanup(snapshot_path)
            manager._process_cleanup_queue()

            assert Path(snapshot_path).exists(), "precondition: delete really failed"
            assert snapshot_path in manager.get_pending_cleanups(), (
                "a failed deletion must not be treated as success -- the path "
                "must stay queued for a later retry"
            )
            pending = {
                row["index_path"] for row in backend.list_cleanup_pending_deletions()
            }
            assert snapshot_path in pending, (
                "a failed deletion must not remove the durable pending row "
                "(that is a permanent, silent disk leak)"
            )
        finally:
            os.chmod(snapshot_path, 0o700)
