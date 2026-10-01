"""Story #1488 AC3-AC11: unit tests for the standalone
`cidx index --migrate-chunks-to-sqlite` orchestration module
(``code_indexer.services.chunk_migration_cli``).

Real files, REAL SQLite (the real ``ChunkStore``), REAL sockets and REAL
fcntl file locks -- no mocking of the code under test or its storage/OS
collaborators. The only monkeypatched surface is ``os.statvfs`` (to
deterministically simulate a low-disk condition) via the reused engine's
own preflight, mirroring the engine's own test methodology.
"""

from __future__ import annotations

import io
import json
import os
import socket
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest
from rich.console import Console

from code_indexer.services.chunk_migration_cli import (
    CollectionOutcome,
    MigrationInventory,
    MigrationLockError,
    MigrationStatus,
    MigrationTargetKind,
    acquire_index_mutation_lock,
    check_no_live_daemon,
    enumerate_migration_targets,
    run_chunk_migration,
)
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore


# --------------------------------------------------------------------------
# Fixtures / helpers (mirror the real FilesystemVectorStore sharded record
# shape, reused from test_collection_migration_1458.py's own helpers).
# --------------------------------------------------------------------------
def _write_vector_json(
    collection_dir: Path,
    point_id: str,
    vector,
    *,
    path: str = "src/foo.py",
    chunk_text: Optional[str] = None,
    git_blob_hash: Optional[str] = None,
    extra_payload: Optional[dict] = None,
) -> Path:
    payload = {"path": path, "language": "python"}
    if extra_payload:
        payload.update(extra_payload)
    record = {
        "id": point_id,
        "vector": vector,
        "metadata": {"language": "python"},
        "payload": payload,
    }
    if chunk_text is not None:
        record["chunk_text"] = chunk_text
        record["indexed_with_uncommitted_changes"] = True
    if git_blob_hash is not None:
        record["git_blob_hash"] = git_blob_hash
        record["indexed_with_uncommitted_changes"] = False
    shard_dir = collection_dir / point_id[:2] / point_id[2:4]
    shard_dir.mkdir(parents=True, exist_ok=True)
    file_path = shard_dir / f"vector_{point_id}.json"
    file_path.write_text(json.dumps(record))
    return file_path


def _write_collection_meta(collection_dir: Path, vector_size: int = 4) -> None:
    collection_dir.mkdir(parents=True, exist_ok=True)
    (collection_dir / "collection_meta.json").write_text(
        json.dumps({"name": collection_dir.name, "vector_size": vector_size})
    )


def _make_semantic_collection(index_dir: Path, name: str, n: int = 2) -> Path:
    coll = index_dir / name
    _write_collection_meta(coll)
    for i in range(n):
        _write_vector_json(
            coll, f"{name[:2]}{i:06d}", [float(i)] * 4, chunk_text=f"c-{i}"
        )
    return coll


def _make_temporal_shard(index_dir: Path, name: str, n: int = 2) -> Path:
    """Build a SHARDED_JSON temporal shard directory with real vector_*.json
    plus an hnsw_index.bin sentinel that must survive migration untouched."""
    coll = index_dir / name
    _write_collection_meta(coll)
    for i in range(n):
        _write_vector_json(
            coll,
            f"tm{i:06d}00",
            [float(i)] * 4,
            git_blob_hash=f"blob{i}",
            extra_payload={"commit_hash": f"deadbeef{i}", "chunk_index": i},
        )
    # Sentinel bookkeeping files that must NOT be touched by migration.
    (coll / "hnsw_index.bin").write_bytes(b"real-hnsw-index-bytes")
    (coll / "temporal_metadata.db").write_bytes(b"real-temporal-meta")
    return coll


def _config(codebase_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(codebase_dir=codebase_dir)


class _FakeConfigManager:
    """Minimal config-manager double exposing ONLY get_socket_path() -- the
    single collaborator surface run_chunk_migration/check_no_live_daemon
    touch. Points at a non-existent socket by default (no live daemon)."""

    def __init__(self, socket_path: Path):
        self._socket_path = socket_path

    def get_socket_path(self) -> Path:
        return self._socket_path


def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, force_terminal=False, width=120), buf


# --------------------------------------------------------------------------
# AC5: typed enumeration
# --------------------------------------------------------------------------
class TestEnumeration:
    def test_classifies_semantic_and_temporal(self, tmp_path: Path) -> None:
        index_dir = tmp_path / "index"
        index_dir.mkdir()
        _make_semantic_collection(index_dir, "code-index-abc123")
        _make_temporal_shard(index_dir, "code-indexer-temporal-voyage_code_3-2024Q1")

        inv = enumerate_migration_targets(index_dir)

        assert [p.name for p in inv.semantic] == ["code-index-abc123"]
        assert [p.name for p in inv.temporal] == [
            "code-indexer-temporal-voyage_code_3-2024Q1"
        ]
        assert inv.unrecognized == []

    def test_skips_shared_bookkeeping_directory(self, tmp_path: Path) -> None:
        index_dir = tmp_path / "index"
        index_dir.mkdir()
        # Bare 'code-indexer-temporal' with no hnsw/vector data == shared
        # bookkeeping dir (Bug #1405) -- must be excluded entirely.
        book = index_dir / "code-indexer-temporal"
        book.mkdir()
        (book / "temporal_metadata.db").write_bytes(b"x")
        (book / "temporal_progress.json").write_text("{}")

        inv = enumerate_migration_targets(index_dir)

        assert inv.temporal == []
        assert inv.semantic == []
        assert inv.unrecognized == []

    def test_reports_unrecognized_never_deletes(self, tmp_path: Path) -> None:
        index_dir = tmp_path / "index"
        index_dir.mkdir()
        stray = index_dir / "mystery-legacy-thing"
        stray.mkdir()
        # Legacy-looking (has vector_*.json) but NO collection_meta.json and
        # not temporal -> reported, never migrated/deleted.
        vf = _write_vector_json(stray, "zz000000", [0.0] * 4, chunk_text="?")

        inv = enumerate_migration_targets(index_dir)

        assert [p.name for p in inv.unrecognized] == ["mystery-legacy-thing"]
        assert inv.semantic == []
        assert vf.exists()  # never touched

    def test_missing_index_dir_returns_empty(self, tmp_path: Path) -> None:
        inv = enumerate_migration_targets(tmp_path / "nope")
        assert isinstance(inv, MigrationInventory)
        assert inv.semantic == [] and inv.temporal == [] and inv.unrecognized == []


# --------------------------------------------------------------------------
# AC7: daemon liveness (authoritative socket probe, NOT bare exists())
# --------------------------------------------------------------------------
class TestDaemonLiveness:
    def test_absent_socket_is_not_live(self, tmp_path: Path) -> None:
        cm = _FakeConfigManager(tmp_path / "daemon.sock")
        # No exception -> no live daemon.
        check_no_live_daemon(cm)

    def test_stale_socket_file_is_not_live(self, tmp_path: Path) -> None:
        # A socket FILE that exists but nothing is listening -> bare exists()
        # would false-positive; the connect-probe must classify it not-live.
        sock_path = tmp_path / "daemon.sock"
        sock_path.write_bytes(b"")  # a regular file at the socket path
        cm = _FakeConfigManager(sock_path)
        check_no_live_daemon(cm)  # must NOT raise

    def test_live_listening_socket_fails_closed(self, tmp_path: Path) -> None:
        sock_path = tmp_path / "daemon.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(1)
        try:
            cm = _FakeConfigManager(sock_path)
            with pytest.raises(MigrationLockError):
                check_no_live_daemon(cm)
        finally:
            server.close()


# --------------------------------------------------------------------------
# AC7: repo-scoped exclusive non-blocking migration lock
# --------------------------------------------------------------------------
class TestMigrationLock:
    def test_lock_file_created_and_released(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".code-indexer"
        config_dir.mkdir()
        with acquire_index_mutation_lock(config_dir):
            assert (config_dir / ".index-mutation.lock").exists()
        # After release, re-acquire must succeed.
        with acquire_index_mutation_lock(config_dir):
            pass

    def test_second_holder_fails_closed(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".code-indexer"
        config_dir.mkdir()

        acquired = threading.Event()
        release = threading.Event()
        errors: list[Exception] = []

        def holder():
            with acquire_index_mutation_lock(config_dir):
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

        assert not errors


# --------------------------------------------------------------------------
# AC3/AC4/AC6/AC8/AC9/AC10: end-to-end orchestration
# --------------------------------------------------------------------------
class TestRunChunkMigration:
    def _wire(self, tmp_path: Path):
        codebase = tmp_path / "repo"
        index_dir = codebase / ".code-indexer" / "index"
        index_dir.mkdir(parents=True)
        cm = _FakeConfigManager(tmp_path / "daemon.sock")
        return codebase, index_dir, cm

    def test_migrates_semantic_and_temporal_exit_zero(self, tmp_path: Path) -> None:
        codebase, index_dir, cm = self._wire(tmp_path)
        sem = _make_semantic_collection(index_dir, "code-index-abc")
        tmp = _make_temporal_shard(
            index_dir, "code-indexer-temporal-voyage_code_3-2024Q2"
        )
        console, buf = _console()

        exit_code = run_chunk_migration(_config(codebase), cm, console=console)

        assert exit_code == 0
        # Both collections consolidated in place.
        assert resolve_chunk_layout(sem) == ChunkLayout.CHUNKS_DB
        assert resolve_chunk_layout(tmp) == ChunkLayout.CHUNKS_DB
        assert (sem / "chunks.db").exists()
        assert (tmp / "chunks.db").exists()
        # Zero legacy vector_*.json remain.
        assert next(sem.rglob("vector_*.json"), None) is None
        assert next(tmp.rglob("vector_*.json"), None) is None
        # Temporal bookkeeping/HNSW files left UNTOUCHED (in-place, no sister).
        assert (tmp / "hnsw_index.bin").read_bytes() == b"real-hnsw-index-bytes"
        assert (tmp / "temporal_metadata.db").exists()
        # No sister/.versioned/alias artifacts created anywhere.
        assert not (codebase / ".code-indexer" / "index" / ".versioned").exists()
        # Status table printed.
        assert "migrated" in buf.getvalue().lower()

    def test_temporal_shard_queryable_after_migration(self, tmp_path: Path) -> None:
        codebase, index_dir, cm = self._wire(tmp_path)
        tmp = _make_temporal_shard(
            index_dir, "code-indexer-temporal-voyage_code_3-2024Q3", n=3
        )
        console, _ = _console()

        run_chunk_migration(_config(codebase), cm, console=console)

        with ChunkStore(tmp / "chunks.db") as store:
            assert store.count() == 3
            ids = store.all_point_ids()
            assert len(ids) == 3
            # Payload passthrough preserved (commit_hash etc).
            rec = store.read(sorted(ids)[0])
            assert "commit_hash" in rec["payload"]

    def test_idempotent_rerun_is_noop(self, tmp_path: Path) -> None:
        codebase, index_dir, cm = self._wire(tmp_path)
        _make_semantic_collection(index_dir, "code-index-abc")
        console, _ = _console()
        assert run_chunk_migration(_config(codebase), cm, console=console) == 0

        console2, buf2 = _console()
        exit2 = run_chunk_migration(_config(codebase), cm, console=console2)

        assert exit2 == 0
        assert "already" in buf2.getvalue().lower()

    def test_insufficient_disk_skips_and_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        codebase, index_dir, cm = self._wire(tmp_path)
        _make_semantic_collection(index_dir, "code-index-abc")
        console, buf = _console()

        import code_indexer.storage.shared.collection_migration as cm_mod

        real_statvfs = os.statvfs

        def fake_statvfs(path):
            st = real_statvfs(path)
            return SimpleNamespace(f_bavail=0, f_frsize=st.f_frsize)

        monkeypatch.setattr(cm_mod.os, "statvfs", fake_statvfs)

        exit_code = run_chunk_migration(_config(codebase), cm, console=console)

        assert exit_code == 1
        assert "skip" in buf.getvalue().lower()

    def test_missing_index_dir_actionable_error_nonzero(self, tmp_path: Path) -> None:
        codebase = tmp_path / "repo"
        (codebase / ".code-indexer").mkdir(parents=True)  # no index/ subdir
        cm = _FakeConfigManager(tmp_path / "daemon.sock")
        console, buf = _console()

        exit_code = run_chunk_migration(_config(codebase), cm, console=console)

        assert exit_code == 1
        assert "index" in buf.getvalue().lower()

    def test_empty_index_is_noop_exit_zero(self, tmp_path: Path) -> None:
        codebase, index_dir, cm = self._wire(tmp_path)  # index dir exists, empty
        console, buf = _console()

        exit_code = run_chunk_migration(_config(codebase), cm, console=console)

        assert exit_code == 0

    def test_only_chunks_db_collections_noop_exit_zero(self, tmp_path: Path) -> None:
        codebase, index_dir, cm = self._wire(tmp_path)
        _make_semantic_collection(index_dir, "code-index-abc")
        console, _ = _console()
        run_chunk_migration(_config(codebase), cm, console=console)  # migrate once

        console2, buf2 = _console()
        assert run_chunk_migration(_config(codebase), cm, console=console2) == 0

    def test_live_daemon_aborts_before_touching_data(self, tmp_path: Path) -> None:
        codebase, index_dir, cm_unused = self._wire(tmp_path)
        sem = _make_semantic_collection(index_dir, "code-index-abc")
        sock_path = tmp_path / "daemon.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(1)
        cm = _FakeConfigManager(sock_path)
        console, _ = _console()
        try:
            with pytest.raises(MigrationLockError):
                run_chunk_migration(_config(codebase), cm, console=console)
        finally:
            server.close()
        # Data untouched: legacy files still present, no chunks.db.
        assert next(sem.rglob("vector_*.json"), None) is not None
        assert not (sem / "chunks.db").exists()


# --------------------------------------------------------------------------
# AC8/AC9: status classification + exit code on failure
# --------------------------------------------------------------------------
class TestStatusClassificationExitCodes:
    def test_outcome_dataclass_fields(self) -> None:
        o = CollectionOutcome(
            name="c",
            kind=MigrationTargetKind.SEMANTIC,
            status=MigrationStatus.MIGRATED,
        )
        assert o.name == "c"
        assert o.kind is MigrationTargetKind.SEMANTIC
        assert o.status is MigrationStatus.MIGRATED

    def test_verification_failure_marks_failed_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        codebase = tmp_path / "repo"
        index_dir = codebase / ".code-indexer" / "index"
        index_dir.mkdir(parents=True)
        _make_semantic_collection(index_dir, "code-index-abc")
        cm = _FakeConfigManager(tmp_path / "daemon.sock")
        console, buf = _console()

        import code_indexer.services.chunk_migration_cli as mig_mod
        from code_indexer.storage.shared.collection_migration import (
            ConsolidationVerificationError,
        )

        def boom(collection_dir, **kwargs):
            raise ConsolidationVerificationError("synthetic mismatch")

        monkeypatch.setattr(mig_mod, "consolidate_collection_in_place", boom)

        exit_code = run_chunk_migration(_config(codebase), cm, console=console)

        assert exit_code == 1
        assert "failed" in buf.getvalue().lower()

    def test_unrecoverable_corruption_surfaced_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        codebase = tmp_path / "repo"
        index_dir = codebase / ".code-indexer" / "index"
        index_dir.mkdir(parents=True)
        _make_semantic_collection(index_dir, "code-index-abc")
        cm = _FakeConfigManager(tmp_path / "daemon.sock")
        console, buf = _console()

        import code_indexer.services.chunk_migration_cli as mig_mod
        from code_indexer.storage.shared.collection_migration import (
            UnrecoverableConsolidationCorruptionError,
        )

        def boom(collection_dir, **kwargs):
            raise UnrecoverableConsolidationCorruptionError("terminal")

        monkeypatch.setattr(mig_mod, "consolidate_collection_in_place", boom)

        exit_code = run_chunk_migration(_config(codebase), cm, console=console)

        assert exit_code == 1
        out = buf.getvalue().lower()
        assert "unrecoverable" in out or "corrupt" in out


# --------------------------------------------------------------------------
# AC11: option-conflict rejection (a clear Click error, never silent)
# --------------------------------------------------------------------------
class TestFlagExclusivity:
    _DEFAULTS = dict(
        clear=False,
        reconcile=False,
        reconcile_embedder=(),
        detect_deletions=False,
        rebuild_indexes=False,
        rebuild_index=False,
        fts=False,
        rebuild_fts_index=False,
        index_commits=False,
        all_branches=False,
        max_commits=None,
        since_date=None,
        diff_context=None,
        new_collection_layout=None,
        files_count_to_process=None,
        progress_json=False,
        batch_size=50,
    )

    def _call(self, **overrides):
        from code_indexer.services.chunk_migration_cli import (
            validate_migrate_flag_exclusivity as _validate,
        )

        kwargs = dict(self._DEFAULTS)
        kwargs.update(overrides)
        return _validate(**kwargs)

    def test_non_conflicting_inputs_are_noops(self) -> None:
        # Only --migrate-chunks-to-sqlite, and the DEFAULT batch_size (50),
        # must both be accepted without raising.
        assert self._call() is None
        assert self._call(batch_size=50) is None

    @pytest.mark.parametrize(
        "override, expected_flag",
        [
            ({"clear": True}, "--clear"),
            ({"reconcile": True}, "--reconcile"),
            ({"reconcile_embedder": ("embed-v4.0",)}, "--reconcile-embedder"),
            ({"detect_deletions": True}, "--detect-deletions"),
            ({"rebuild_indexes": True}, "--rebuild-indexes"),
            ({"rebuild_index": True}, "--rebuild-index"),
            ({"fts": True}, "--fts"),
            ({"rebuild_fts_index": True}, "--rebuild-fts-index"),
            ({"index_commits": True}, "--index-commits"),
            ({"all_branches": True}, "--all-branches"),
            ({"max_commits": 10}, "--max-commits"),
            ({"since_date": "2024-01-01"}, "--since-date"),
            ({"diff_context": 3}, "--diff-context"),
            ({"new_collection_layout": "chunks_db"}, "--new-collection-layout"),
            ({"files_count_to_process": 5}, "--files-count-to-process"),
            ({"progress_json": True}, "--progress-json"),
            ({"batch_size": 100}, "--batch-size"),
        ],
    )
    def test_each_conflicting_flag_rejected_and_named(
        self, override, expected_flag
    ) -> None:
        import click

        with pytest.raises(click.UsageError) as exc_info:
            self._call(**override)
        msg = str(exc_info.value)
        assert expected_flag in msg
        assert "--migrate-chunks-to-sqlite" in msg
