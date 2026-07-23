"""Integration test: SQLite chunk-store engine round-trip proof against a
real NFS mount (Story #1455, Epic #1454, AC6).

AC6 requires round-trip correctness to be proven against BOTH local disk AND
a real NFS-mounted volume -- local disk is already covered by
``tests/unit/storage/test_sqlite_chunk_store.py``'s unit suite (pytest's
``tmp_path`` fixture allocates on local disk; every test there round-trips
correctly). This file supplies the NFS half of that gate.

Gating
------
This file is gated by the ``CIDX_CHUNK_STORE_NFS_TEST_DIR`` environment
variable, which must point at a directory on a REAL, live NFS mount. The
suite is SKIPPED entirely when the variable is unset, and FAILS loudly (not
silently skipped) if the variable is set but the target path does not
resolve to an NFS-mounted filesystem -- no CI runner or local dev machine
has such a mount, so this file is intentionally NOT part of
fast-automation.sh / server-fast-automation.sh / e2e-automation.sh. Run it
from a host that has a real NFS mount available, pointing the environment
variable at an isolated subdirectory of that mount. Environment-specific
host/path details belong in operator documentation, not in this source
file.

What each test class proves
----------------------------
- ``TestNFSRoundTripCorrectness``: the AC6 MANDATORY pass/fail gate --
  every field, including the vector bytes, round-trips byte-identically
  through a full close/reopen cycle on the real NFS mount.
- ``TestNFSTimingDiagnostics``: read/write timing metrics for NFS vs local
  disk, recorded as diagnostic output only -- per AC6, "do NOT assert
  against a hardcoded numeric threshold" (the spike's reference numbers
  were viability evidence, never an enforced gate).
- ``TestNFSInterruptedTransactionRollbackConsistency``: reproduces, on the
  real NFS mount, an interrupted write transaction (analogous to a
  transient NFS "disk I/O error" aborting an in-flight write) and proves
  the store recovers to an atomically consistent state -- zero rows from
  the interrupted transaction, zero torn/corrupt records -- when reopened
  through the real production ``ChunkStore`` API. The interruption is
  induced deterministically via a child process that SIGKILLs itself
  mid-transaction (before COMMIT), which only SQLite's automatic
  hot-journal rollback (triggered on the next connection) can resolve --
  the same recovery mechanism that protects against a genuine NFS write
  hiccup.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pytest

from code_indexer.storage.sqlite_chunk_store import ChunkStore

NFS_TEST_DIR_ENV = "CIDX_CHUNK_STORE_NFS_TEST_DIR"


def _mount_fstype(path: Path) -> str:
    """Return the filesystem type backing ``path`` by finding the
    longest-matching mount point in ``/proc/mounts`` (Linux-only, matching
    this project's server/staging fleet).
    """
    resolved = str(path.resolve())
    best_match = ""
    best_fstype = ""
    with open("/proc/mounts") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 3:
                continue
            mount_point, fstype = parts[1], parts[2]
            if resolved == mount_point or resolved.startswith(
                mount_point.rstrip("/") + "/"
            ):
                if len(mount_point) > len(best_match):
                    best_match = mount_point
                    best_fstype = fstype
    return best_fstype


def _nfs_test_dir_is_configured() -> bool:
    return bool(os.environ.get(NFS_TEST_DIR_ENV))


pytestmark = pytest.mark.skipif(
    not _nfs_test_dir_is_configured(),
    reason=(
        f"{NFS_TEST_DIR_ENV} not set -- this test requires a real, live "
        "NFS mount and is not run in CI."
    ),
)


def _nfs_dir() -> Path:
    d = Path(os.environ[NFS_TEST_DIR_ENV])
    d.mkdir(parents=True, exist_ok=True)
    fstype = _mount_fstype(d)
    if not fstype.startswith("nfs"):
        pytest.fail(
            f"{NFS_TEST_DIR_ENV}={d} does not resolve to an NFS-mounted "
            f"filesystem (detected fstype={fstype!r}). AC6 requires this "
            "suite to run against a real NFS mount, not local disk."
        )
    return d


def _clean(db_path: Path) -> None:
    db_path.unlink(missing_ok=True)
    Path(str(db_path) + "-journal").unlink(missing_ok=True)


VECTOR_DIM = 1024  # matches production voyage-code-3 dimension


def _make_vector(seed: int) -> List[float]:
    rng = np.random.RandomState(seed)
    result: List[float] = rng.rand(VECTOR_DIM).astype(np.float32).tolist()
    return result


def _make_record(i: int) -> Dict[str, Any]:
    return {
        "id": f"nfs-proof-{i}",
        "vector": _make_vector(i),
        "metadata": {"language": "python", "type": "content"},
        "payload": {
            "path": f"src/file_{i}.py",
            "line_start": i,
            "line_end": i + 10,
            "hidden_branches": [] if i % 3 else ["feature/x"],
        },
        "chunk_text": f"def f_{i}():\n    return {i}\n",
    }


class TestNFSRoundTripCorrectness:
    """AC6 MANDATORY pass/fail gate: round-trip correctness on the real
    NFS mount -- every field, including raw vector bytes, byte-identical
    after a full close/reopen cycle."""

    def test_round_trip_byte_identical_on_nfs(self):
        db_path = _nfs_dir() / "roundtrip_proof.db"
        _clean(db_path)

        records = [_make_record(i) for i in range(200)]

        with ChunkStore(db_path) as store:
            store.write_batch(records)

        # Fresh connection (process-equivalent reopen) -- proves durability
        # on the NFS mount, not just in-memory-cache correctness.
        with ChunkStore(db_path) as store:
            assert store.count() == 200
            for original in records:
                result = store.read(original["id"])
                assert result is not None
                assert result["payload"] == original["payload"]
                assert result["metadata"] == original["metadata"]
                assert result["chunk_text"] == original["chunk_text"]
                assert (
                    np.asarray(result["vector"], dtype="<f4").tobytes()
                    == np.asarray(original["vector"], dtype="<f4").tobytes()
                )

        _clean(db_path)


class TestNFSTimingDiagnostics:
    """Diagnostic-only timing metrics on NFS vs local disk.

    Per AC6: 'record read/write timing metrics ... as diagnostic output; do
    NOT assert against a hardcoded numeric threshold.' No pass/fail
    assertion is made on the numbers -- only that both environments
    complete the operations and return correct results.
    """

    def test_write_and_read_timing_nfs_vs_local(self, tmp_path):
        n = 1000
        records = [_make_record(i) for i in range(n)]

        def _measure(db_path: Path) -> Dict[str, float]:
            _clean(db_path)

            t0 = time.monotonic()
            with ChunkStore(db_path) as store:
                store.write_batch(records)
            write_s = time.monotonic() - t0

            t0 = time.monotonic()
            with ChunkStore(db_path) as store:
                for r in records:
                    assert store.read(r["id"]) is not None
            read_s = time.monotonic() - t0

            _clean(db_path)
            return {"write_s": write_s, "read_1000_s": read_s}

        nfs_metrics = _measure(_nfs_dir() / "timing_proof.db")
        local_metrics = _measure(tmp_path / "timing_proof_local.db")

        print(f"\n[AC6 DIAGNOSTIC] n={n} records")
        print(
            f"[AC6 DIAGNOSTIC] NFS   write={nfs_metrics['write_s']:.3f}s "
            f"read({n})={nfs_metrics['read_1000_s']:.3f}s"
        )
        print(
            f"[AC6 DIAGNOSTIC] LOCAL write={local_metrics['write_s']:.3f}s "
            f"read({n})={local_metrics['read_1000_s']:.3f}s"
        )
        # No threshold assertion -- recorded reference evidence
        # establishing viability (per AC6), never an enforced gate.


# Fixed, constant child-process source (never interpolated with
# env/user-derived data -- the target db path is passed via argv, not
# embedded in the script text, to avoid any code-injection risk).
_INTERRUPT_CHILD_SCRIPT = """\
import sqlite3, os, signal, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("PRAGMA journal_mode=DELETE")
conn.execute("BEGIN")
for i in range(500):
    conn.execute(
        "INSERT OR REPLACE INTO chunks (point_id, path, vector, data) VALUES (?, ?, ?, ?)",
        ("interrupted-" + str(i), "x", b"\\x00" * 4096, b"\\x00" * 64),
    )
    if i == 250:
        os.kill(os.getpid(), signal.SIGKILL)
conn.commit()
"""


class TestNFSInterruptedTransactionRollbackConsistency:
    """Reproduces, on the real NFS mount, a write transaction interrupted
    before COMMIT (an NFS write hiccup manifests to SQLite as exactly this
    kind of abrupt interruption). Proves the chunk store is atomically
    consistent afterward -- zero rows from the interrupted transaction,
    zero torn/corrupt records -- when reopened through the real
    ``ChunkStore`` API.
    """

    def test_sigkill_mid_transaction_leaves_store_consistent_on_nfs(self):
        db_path = _nfs_dir() / "interrupt_proof.db"
        _clean(db_path)

        baseline = [_make_record(i) for i in range(1000, 1050)]
        with ChunkStore(db_path) as store:
            store.write_batch(baseline)
            assert store.count() == 50

        # Child process: opens the SAME db file on the SAME NFS mount,
        # starts a large explicit transaction against the real `chunks`
        # table schema, and SIGKILLs itself partway through -- guaranteed
        # to happen before COMMIT is ever reached. Raw sqlite3 (not
        # ChunkStore.write_batch) is used deliberately here: write_batch is
        # a single executemany+commit with no interruptible midpoint --
        # exactly as it should be. This drives the same journal_mode=DELETE
        # schema the module uses to control WHERE the interruption lands.
        # The db path is passed as argv[1], never interpolated into the
        # script source itself.
        proc = subprocess.run(
            [sys.executable, "-c", _INTERRUPT_CHILD_SCRIPT, str(db_path)],
            capture_output=True,
            timeout=30,
        )
        # SIGKILL -> negative returncode (-9) on POSIX.
        assert proc.returncode == -signal.SIGKILL, (
            "child did not die by SIGKILL as expected: "
            f"returncode={proc.returncode} stdout={proc.stdout!r} "
            f"stderr={proc.stderr!r}"
        )

        # Reopen through the REAL production API -- this is the exact code
        # path a later caller (indexing pipeline, HNSW rebuild, etc.) would
        # use. SQLite's automatic hot-journal rollback must have already
        # restored consistency by the time this succeeds.
        with ChunkStore(db_path) as store:
            assert store.count() == 50, (
                "interrupted transaction's rows must NOT be visible -- "
                "the store must roll back to the last committed state"
            )
            for original in baseline:
                result = store.read(original["id"])
                assert result is not None
                assert result["payload"] == original["payload"]
            # None of the interrupted transaction's rows exist.
            assert store.read("interrupted-0") is None
            assert store.read("interrupted-249") is None

        _clean(db_path)
