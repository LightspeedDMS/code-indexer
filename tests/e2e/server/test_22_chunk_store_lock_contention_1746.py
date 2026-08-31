"""Phase 3 — Bug #1746 code review finding B3: REST front-door E2E proof
that transient sqlite lock contention on a real chunks.db, held by a real
separate process/connection, does NOT abort a real server-triggered
reindex job.

Root cause this test guards against: FilesystemVectorStore.preflight_
chunk_store_writable() used to raise ChunkStoreUnavailableError on ANY
exception from opening chunks.db -- a second classification site that
bypassed the is_fatal_chunk_store_write_error() classifier Change 1/H1
built for the per-file write path. A real EXCLUSIVE lock held by a
concurrent writer (expected under real production concurrency -- the
CHUNKS_DB write path opens a fresh connection per upsert_points() call
with no cross-thread application lock) would abort the ENTIRE reindex job
on purely transient contention. Since M1 moved the preflight check to fire
earliest in the run, this bug was MORE likely to trigger, not less.

Uses the REAL REST front door end-to-end:
  1. seeded_indexed_client (session fixture) registers + activates a real
     golden repo via POST /api/admin/golden-repos + POST /api/repos/activate.
  2. This test resolves the REAL on-disk chunks.db for that activation via
     ActivatedRepoManager.get_activated_repo_path() (aligned to
     CIDX_SERVER_DATA_DIR, same technique test_15_gitwrite_globalalias_1135.py
     already uses).
  3. A REAL second sqlite3 connection holds a genuine BEGIN EXCLUSIVE lock
     on that chunks.db from a background thread, for longer than sqlite's
     default 5s busy-timeout.
  4. POST /api/activated-repos/{alias}/reindex triggers a REAL background
     reindex job (the exact server code path a production activate/sync/
     reindex request takes: ActivatedRepoIndexManager -> a real `cidx
     index` subprocess -> SmartIndexer -> the same preflight check).
  5. GET /api/jobs/{job_id} is polled to a terminal state.

Assertion: the job must reach status="completed" -- NOT "failed" -- once
the transient lock naturally releases. A pre-B3-fix server would fail the
job almost immediately (within the ~5s busy-timeout window) with a
ChunkStoreUnavailableError about "database is locked".
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Tuple

import pytest
from fastapi.testclient import TestClient

# Held longer than sqlite3's default 5.0s busy-timeout so the reindex
# job's preflight check (and any per-file write attempted before the lock
# releases) genuinely observes "database is locked", not a lucky race.
_LOCK_HOLD_SECONDS = 7.0

_JOB_TIMEOUT: float = float(os.environ.get("E2E_GOLDEN_JOB_TIMEOUT", "300"))
_JOB_POLL_INTERVAL: float = float(os.environ.get("E2E_GOLDEN_JOB_POLL", "0.5"))
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


def _wait_for_terminal_job(
    client: TestClient, job_id: str, auth_headers: dict
) -> dict[str, Any]:
    """Poll GET /api/jobs/{job_id} until a terminal state; return the body.

    Bounded loop (Messi Rule #14): terminates on deadline (TimeoutError)
    or terminal state. Does NOT assert success -- the caller decides.
    """
    deadline = time.monotonic() + _JOB_TIMEOUT
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}", headers=auth_headers)
        assert resp.status_code < 500, (
            f"Job poll returned HTTP {resp.status_code}: {resp.text[:300]}"
        )
        if resp.status_code == 200:
            body: dict[str, Any] = resp.json()
            if body.get("status") in _TERMINAL_STATES:
                return body
        time.sleep(_JOB_POLL_INTERVAL)
    raise TimeoutError(
        f"Job {job_id!r} did not reach a terminal state within {_JOB_TIMEOUT}s"
    )


def _find_real_chunks_db(activated_repo_path: Path) -> Path:
    """Locate the real, already-indexed chunks.db under the activated
    repo's .code-indexer/index/ tree -- walks rather than hardcoding the
    provider/model directory name so this stays robust across providers."""
    index_root = activated_repo_path / ".code-indexer" / "index"
    assert index_root.exists(), f"expected real index dir at {index_root}"
    candidates = list(index_root.glob("*/chunks.db"))
    assert candidates, (
        f"expected a real, already-indexed chunks.db under {index_root} "
        f"(seeded_indexed_client should have produced one) -- found none"
    )
    return candidates[0]


@pytest.fixture
def _activated_repo_manager(test_client_data_dir: Path) -> Any:
    """Real ActivatedRepoManager aligned to CIDX_SERVER_DATA_DIR -- same
    technique as test_15_gitwrite_globalalias_1135.py's
    _arm_data_dir_aligned fixture, applied directly via constructor arg
    instead of a monkeypatch (no other test in this session needs the
    no-arg constructor to be aligned)."""
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    return ActivatedRepoManager(
        data_dir=os.path.join(str(test_client_data_dir), "data")
    )


class TestBug1746B3TransientLockContentionDoesNotAbortRealJob:
    """AC (code review finding B3, verified through the real REST front
    door): a real, separate-process-held EXCLUSIVE lock on chunks.db must
    NOT abort a real server-triggered reindex job -- the job must
    complete once the transient contention clears."""

    def test_reindex_job_completes_despite_transient_lock_contention(
        self,
        seeded_indexed_client: Tuple[TestClient, str],
        _activated_repo_manager: Any,
        admin_token_provider: Any,
    ) -> None:
        client, alias = seeded_indexed_client
        username = os.environ["E2E_ADMIN_USER"]

        repo_path = Path(
            _activated_repo_manager.get_activated_repo_path(username, alias)
        )
        assert repo_path.exists(), f"expected real activated repo at {repo_path}"

        chunks_db_path = _find_real_chunks_db(repo_path)

        # Hold a REAL exclusive lock from a separate connection/thread --
        # exactly the transient contention shape a concurrent CHUNKS_DB
        # writer produces in production. Released well before the test's
        # own job-poll deadline, simulating the lock naturally clearing.
        lock_acquired = threading.Event()

        def _hold_lock() -> None:
            conn = sqlite3.connect(str(chunks_db_path))
            conn.execute("BEGIN EXCLUSIVE")
            lock_acquired.set()
            time.sleep(_LOCK_HOLD_SECONDS)
            conn.execute("ROLLBACK")
            conn.close()

        lock_thread = threading.Thread(target=_hold_lock, daemon=True)
        lock_thread.start()
        assert lock_acquired.wait(timeout=5.0), (
            "lock-holder thread never acquired the lock"
        )

        try:
            auth_headers = admin_token_provider.get_headers()
            resp = client.post(
                f"/api/activated-repos/{alias}/reindex",
                json={"index_types": ["semantic"]},
                headers=auth_headers,
            )
            assert resp.status_code == 202, (
                f"reindex trigger failed: HTTP {resp.status_code} -- {resp.text[:300]}"
            )
            body = resp.json()
            job_id = body.get("job_id")
            assert job_id, f"reindex response missing job_id: {body}"

            status = _wait_for_terminal_job(
                client, job_id, admin_token_provider.get_headers()
            )
        finally:
            lock_thread.join(timeout=_LOCK_HOLD_SECONDS + 5.0)

        assert status.get("status") == "completed", (
            f"Bug #1746 B3 regression: reindex job wrongly reported "
            f"status={status.get('status')!r} on transient sqlite lock "
            f"contention instead of completing once the lock released -- "
            f"full status: {status}"
        )
