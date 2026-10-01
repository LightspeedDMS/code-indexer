"""Story #1488 adversarial-review (Codex) remediation for the standalone
``cidx index --migrate-chunks-to-sqlite`` orchestration
(``code_indexer.services.chunk_migration_cli``).

Real files, REAL SQLite, REAL sockets and REAL fcntl locks -- no mocking of
the code under test. Covers:

  * Finding 6 -- daemon-liveness probe must fail CLOSED on an INDETERMINATE
    result (EACCES / timeout / any non-definitive OSError): only ENOENT and
    ECONNREFUSED are definitive "not live". An EACCES (mode-000) live socket
    must make ``check_no_live_daemon`` raise, never silently proceed.
  * Finding 7 -- the bare ``code-indexer-temporal`` directory is NEVER a
    migratable shard. Excluded UNCONDITIONALLY by exact name; if it holds
    vector-looking data it is surfaced as an operator-visible anomaly (never
    migrated, never deleted).
  * Finding 8 -- an index containing ONLY an unrecognized/anomaly directory
    must exit NON-ZERO and report it, leaving the directory untouched (never
    an empty table + exit 0 while old-format data remains).
  * Finding 4(b) -- a RAW (untyped) error escaping a single collection's
    consolidation must be recorded as that collection's FAILED outcome and
    the command must STILL print the final status table and return non-zero,
    never abort the whole command before the table.
  * Finding 1(a) -- the migration guard is renamed to the shared index-
    mutation lock (``acquire_index_mutation_lock`` / ``.index-mutation.lock``)
    and still fails closed on a concurrent acquire.
"""

from __future__ import annotations

import io
import json
import os
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from code_indexer.services.chunk_migration_cli import (
    INDEX_MUTATION_LOCK_FILENAME,
    MigrationLockError,
    acquire_index_mutation_lock,
    check_no_live_daemon,
    enumerate_migration_targets,
    run_chunk_migration,
)
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _write_vector_json(collection_dir: Path, point_id: str, vector) -> Path:
    record = {
        "id": point_id,
        "vector": vector,
        "metadata": {"language": "python"},
        "payload": {"path": "src/foo.py", "language": "python"},
        "chunk_text": "chunk",
        "indexed_with_uncommitted_changes": True,
    }
    shard_dir = collection_dir / point_id[:2] / point_id[2:4]
    shard_dir.mkdir(parents=True, exist_ok=True)
    fp = shard_dir / f"vector_{point_id}.json"
    fp.write_text(json.dumps(record))
    return fp


def _write_collection_meta(collection_dir: Path, vector_size: int = 4) -> None:
    collection_dir.mkdir(parents=True, exist_ok=True)
    (collection_dir / "collection_meta.json").write_text(
        json.dumps({"name": collection_dir.name, "vector_size": vector_size})
    )


def _make_semantic_collection(index_dir: Path, name: str, n: int = 2) -> Path:
    coll = index_dir / name
    _write_collection_meta(coll)
    for i in range(n):
        _write_vector_json(coll, f"{name[:2]}{i:06d}", [float(i)] * 4)
    return coll


class _FakeConfigManager:
    def __init__(self, socket_path: Path):
        self._socket_path = socket_path

    def get_socket_path(self) -> Path:
        return self._socket_path


def _config(codebase_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(codebase_dir=codebase_dir)


def _console() -> "tuple[Console, io.StringIO]":
    buf = io.StringIO()
    return Console(file=buf, force_terminal=False, width=120), buf


def _wire(tmp_path: Path):
    codebase = tmp_path / "repo"
    index_dir = codebase / ".code-indexer" / "index"
    index_dir.mkdir(parents=True)
    cm = _FakeConfigManager(tmp_path / "daemon.sock")
    return codebase, index_dir, cm


# --------------------------------------------------------------------------
# Finding 1(a): the renamed index-mutation lock.
# --------------------------------------------------------------------------
class TestIndexMutationLockRename:
    def test_lock_filename_is_index_mutation_lock(self) -> None:
        assert INDEX_MUTATION_LOCK_FILENAME == ".index-mutation.lock"

    def test_lock_created_and_second_holder_fails_closed(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".code-indexer"
        config_dir.mkdir()

        acquired = threading.Event()
        release = threading.Event()

        def holder():
            with acquire_index_mutation_lock(config_dir):
                assert (config_dir / ".index-mutation.lock").exists()
                acquired.set()
                release.wait(timeout=5)

        t = threading.Thread(target=holder)
        t.start()
        try:
            assert acquired.wait(timeout=5)
            with pytest.raises(MigrationLockError):
                with acquire_index_mutation_lock(config_dir):
                    pass
        finally:
            release.set()
            t.join(timeout=5)


# --------------------------------------------------------------------------
# Finding 6: daemon liveness fails CLOSED on indeterminate (EACCES).
# --------------------------------------------------------------------------
class TestDaemonLivenessFailsClosed:
    def test_eacces_socket_fails_closed(self, tmp_path: Path) -> None:
        sock_path = tmp_path / "daemon.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(1)
        os.chmod(sock_path, 0)  # EACCES on connect -> INDETERMINATE
        cm = _FakeConfigManager(sock_path)
        try:
            if os.geteuid() == 0:
                pytest.skip("running as root: EACCES not enforced on the socket")
            with pytest.raises(MigrationLockError):
                check_no_live_daemon(cm)
        finally:
            os.chmod(sock_path, 0o600)
            server.close()

    def test_absent_socket_still_not_live(self, tmp_path: Path) -> None:
        cm = _FakeConfigManager(tmp_path / "nope.sock")
        check_no_live_daemon(cm)  # ENOENT -> definitive not-live, no raise

    def test_stale_regular_file_still_not_live(self, tmp_path: Path) -> None:
        sock_path = tmp_path / "daemon.sock"
        sock_path.write_bytes(b"")  # ECONNREFUSED on connect -> not live
        cm = _FakeConfigManager(sock_path)
        check_no_live_daemon(cm)  # must NOT raise


# --------------------------------------------------------------------------
# Finding 7: bare code-indexer-temporal WITH data -> anomaly, never migrated.
# --------------------------------------------------------------------------
class TestBareTemporalWithDataIsAnomaly:
    def test_bare_temporal_with_vector_data_excluded_and_reported(
        self, tmp_path: Path
    ) -> None:
        index_dir = tmp_path / "index"
        index_dir.mkdir()
        bare = index_dir / "code-indexer-temporal"
        _write_collection_meta(bare)
        _write_vector_json(bare, "tm000000", [1.0, 2.0, 3.0, 4.0])
        (bare / "hnsw_index.bin").write_bytes(b"real-hnsw")

        inv = enumerate_migration_targets(index_dir)

        # Never a migration target.
        assert bare not in inv.temporal
        assert bare not in inv.semantic
        # Surfaced as an anomaly.
        assert bare in inv.anomalies

    def test_bare_temporal_with_data_run_exits_nonzero_untouched(
        self, tmp_path: Path
    ) -> None:
        codebase, index_dir, cm = _wire(tmp_path)
        bare = index_dir / "code-indexer-temporal"
        _write_collection_meta(bare)
        vf = _write_vector_json(bare, "tm000000", [1.0, 2.0, 3.0, 4.0])
        console, buf = _console()

        exit_code = run_chunk_migration(_config(codebase), cm, console=console)

        assert exit_code != 0
        # Never migrated/deleted.
        assert vf.exists()
        assert not (bare / "chunks.db").exists()
        out = buf.getvalue().lower()
        assert "code-indexer-temporal" in out


# --------------------------------------------------------------------------
# Finding 8: only-unrecognized dir -> non-zero exit + reported, untouched.
# --------------------------------------------------------------------------
class TestUnrecognizedCausesNonZeroExit:
    def test_only_unrecognized_dir_exits_nonzero_and_reports(
        self, tmp_path: Path
    ) -> None:
        codebase, index_dir, cm = _wire(tmp_path)
        stray = index_dir / "mystery-legacy-thing"
        stray.mkdir()
        vf = _write_vector_json(stray, "zz000000", [0.0] * 4)
        console, buf = _console()

        exit_code = run_chunk_migration(_config(codebase), cm, console=console)

        assert exit_code != 0
        assert vf.exists()  # untouched
        assert "mystery-legacy-thing" in buf.getvalue()


# --------------------------------------------------------------------------
# Finding 4(b): a RAW error per-collection -> FAILED + table + non-zero.
# --------------------------------------------------------------------------
class TestRawErrorStillPrintsTable:
    def test_raw_error_recorded_failed_table_printed_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        codebase, index_dir, cm = _wire(tmp_path)
        _make_semantic_collection(index_dir, "code-index-boom")
        ok = _make_semantic_collection(index_dir, "code-index-ok")
        console, buf = _console()

        import code_indexer.services.chunk_migration_cli as mig_mod

        real = mig_mod.consolidate_collection_in_place

        def maybe_boom(collection_dir, **kwargs):
            if Path(collection_dir).name == "code-index-boom":
                # A RAW, untyped error (NOT a Consolidation* type).
                raise RuntimeError("synthetic raw failure")
            return real(collection_dir, **kwargs)

        monkeypatch.setattr(mig_mod, "consolidate_collection_in_place", maybe_boom)

        exit_code = run_chunk_migration(_config(codebase), cm, console=console)

        assert exit_code != 0
        out = buf.getvalue().lower()
        # The final status table still printed (never aborted before it), and
        # the healthy collection still migrated.
        assert "failed" in out
        assert resolve_chunk_layout(ok) == ChunkLayout.CHUNKS_DB
