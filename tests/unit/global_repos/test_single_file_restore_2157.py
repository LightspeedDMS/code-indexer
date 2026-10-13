"""Bug #2157: a single-file restore (a collection's ``chunks.db`` and every
``metadata*.json``) works the same on every deployment mode.

On a cluster node the clone backend is the CoW daemon, which clones
DIRECTORIES through a daemon job. Handed a single file it wrote the file
(owned by the daemon's user), then failed the job with "Path is not a
directory", so the self-heal reported a restored store as "not restorable".
The restore must not go through the clone backend at all: it copies the file
atomically (temporary sibling, fsync, ``os.replace``) from this process.

Real files and real SQLite stores throughout. Copy failures are injected by
wrapping ``shutil.copyfileobj``.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import List, Tuple

import pytest

from code_indexer.global_repos.refresh_failure_recovery import (
    run_integrity_gate_against_published,
)
from code_indexer.global_repos.refresh_integrity_gate import (
    RestoreNotDurableError,
    restore_chunks_db_file,
    restore_metadata_files,
    run_refresh_integrity_gate,
)
from tests.unit.global_repos.test_refresh_integrity_gate_1506 import (
    _flip_bytes_at_midpoint,
    _make_chunks_db_collection,
    _real_records,
)

_METADATA_NAME = "metadata-voyage-ai.json"


def _published_and_source(tmp_path: Path) -> Tuple[Path, Path]:
    """A published snapshot (store + metadata at the old commit) and a source
    clone whose established collection has lost its store while its metadata
    claims a newer commit. Returns (published_root, source_root)."""
    published = tmp_path / "versioned" / "v_1"
    published_meta_dir = published / ".code-indexer"
    _make_chunks_db_collection(published_meta_dir / "index" / "coll", _real_records(40))
    (published_meta_dir / _METADATA_NAME).write_text(
        json.dumps({"status": "completed", "current_commit": "old_sha"})
    )

    source = tmp_path / "source"
    source_meta_dir = source / ".code-indexer"
    source_db = _make_chunks_db_collection(
        source_meta_dir / "index" / "coll", _real_records(40)
    )
    source_db.unlink()
    (source_meta_dir / _METADATA_NAME).write_text(
        json.dumps({"status": "completed", "current_commit": "new_sha"})
    )
    return published, source


def _fail_copy_halfway(monkeypatch: pytest.MonkeyPatch) -> None:
    """The copy writes part of the data, then the disk fills up."""

    def copy_half_then_fail(source, destination, *args) -> None:
        destination.write(source.read(16))
        destination.flush()
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copyfileobj", copy_half_then_fail)


def _names_in(directory: Path) -> List[str]:
    return sorted(p.name for p in directory.iterdir())


def _hot_journal_of_other_database(work_dir: Path) -> Path:
    """A genuine hot rollback journal of an unrelated database: a write
    transaction modifies many pages, and its journal is copied away before
    the transaction ends (as if the writer had been killed). Returns the copy.

    synchronous=OFF makes SQLite size the journal by its file length, so every
    journaled page is played back on the next open."""
    work_dir.mkdir(parents=True)
    other_db = work_dir / "other.db"
    journal_copy = work_dir / "hot-journal"
    conn = sqlite3.connect(other_db, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE other_rows (k INTEGER, v TEXT)")
        conn.executemany(
            "INSERT INTO other_rows VALUES (?, ?)",
            [(i, "o" * 300) for i in range(800)],
        )
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("BEGIN")
        conn.execute("UPDATE other_rows SET v = 'changed' || v")
        shutil.copyfile(work_dir / "other.db-journal", journal_copy)
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    assert journal_copy.stat().st_size > 0
    return journal_copy


def _verified_dump(db_path: Path, *, read_only: bool) -> List[str]:
    """Open *db_path* as SQLite would on the next run (read-write plays back a
    hot journal beside it), require integrity_check ok, and dump it."""
    uri = db_path.resolve().as_uri() + ("?mode=ro" if read_only else "")
    conn = sqlite3.connect(uri, uri=True)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        return list(conn.iterdump())
    finally:
        conn.close()


def _fail_directory_fsync(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    """fsync of *directory* itself fails with EIO; every other fsync (files,
    other directories) is real."""
    real_fsync = os.fsync
    target = str(directory.resolve())

    def fsync(fd: int) -> None:
        if os.readlink(f"/proc/self/fd/{fd}") == target:
            raise OSError(errno.EIO, "Input/output error")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)


class TestReplacedButNotDurable:
    def test_gate_verifies_and_restores_metadata_when_directory_fsync_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        published, source = _published_and_source(tmp_path)
        source_coll = source / ".code-indexer" / "index" / "coll"
        _fail_directory_fsync(monkeypatch, source_coll)

        with caplog.at_level("ERROR"):
            gate = run_integrity_gate_against_published(
                str(source), str(published), restore_missing_stores=True
            )

        published_db = published / ".code-indexer" / "index" / "coll" / "chunks.db"
        assert (source_coll / "chunks.db").read_bytes() == published_db.read_bytes()
        restored_meta = source / ".code-indexer" / _METADATA_NAME
        assert json.loads(restored_meta.read_text())["current_commit"] == "old_sha"
        failure = gate.failures[0]
        assert failure.metadata_restored_files == [restored_meta]
        assert failure.metadata_restore_error is None
        assert gate.passed is False
        assert failure.self_heal_succeeded is False
        assert failure.self_heal_error is not None
        assert "not durable" in failure.self_heal_error
        assert any(
            "not durable" in r.getMessage() and "OSError" in r.getMessage()
            for r in caplog.records
            if r.levelname == "ERROR"
        )

    def test_metadata_restore_replaces_every_file_then_reports_not_durable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        healthy_dir = tmp_path / "healthy"
        healthy_dir.mkdir()
        (healthy_dir / "metadata-cohere.json").write_text('{"c": "old_b"}')
        (healthy_dir / _METADATA_NAME).write_text('{"c": "old_a"}')
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        (target_dir / _METADATA_NAME).write_text('{"c": "new_a"}')
        _fail_directory_fsync(monkeypatch, target_dir)

        with pytest.raises(RestoreNotDurableError) as raised:
            restore_metadata_files(healthy_dir, target_dir)

        assert raised.value.replaced is True
        assert raised.value.replaced_paths == [
            target_dir / "metadata-cohere.json",
            target_dir / _METADATA_NAME,
        ]
        assert (target_dir / "metadata-cohere.json").read_text() == '{"c": "old_b"}'
        assert (target_dir / _METADATA_NAME).read_text() == '{"c": "old_a"}'


class TestStaleSqliteSidecars:
    def test_hot_journal_of_discarded_database_never_rolls_into_restored_store(
        self, tmp_path: Path
    ) -> None:
        snapshot_db = _make_chunks_db_collection(
            tmp_path / "published" / "coll", _real_records(40)
        )
        target_db = _make_chunks_db_collection(
            tmp_path / "source" / "coll", _real_records(10)
        )
        hot_journal = _hot_journal_of_other_database(tmp_path / "other")
        shutil.copyfile(hot_journal, target_db.with_name("chunks.db-journal"))
        expected = _verified_dump(snapshot_db, read_only=True)

        restore_chunks_db_file(snapshot_db, target_db)

        assert _verified_dump(target_db, read_only=False) == expected
        assert "chunks.db-journal" not in _names_in(target_db.parent)

    def test_gate_restores_missing_store_beside_hot_journal(
        self, tmp_path: Path
    ) -> None:
        published, source = _published_and_source(tmp_path)
        source_coll = source / ".code-indexer" / "index" / "coll"
        hot_journal = _hot_journal_of_other_database(tmp_path / "other")
        shutil.copyfile(hot_journal, source_coll / "chunks.db-journal")
        published_db = published / ".code-indexer" / "index" / "coll" / "chunks.db"
        expected_rows = _verified_dump(published_db, read_only=True)

        gate = run_integrity_gate_against_published(
            str(source), str(published), restore_missing_stores=True
        )

        failure = gate.failures[0]
        assert failure.self_heal_succeeded is True, failure.self_heal_error
        restored_db = source_coll / "chunks.db"
        assert _verified_dump(restored_db, read_only=False) == expected_rows
        assert restored_db.read_bytes() == published_db.read_bytes()

    @pytest.mark.parametrize("suffix", ["-wal", "-shm"])
    def test_wal_and_shm_of_discarded_database_removed(
        self, tmp_path: Path, suffix: str
    ) -> None:
        source = tmp_path / "healthy.db"
        source.write_bytes(b"healthy content " * 64)
        target_dir = tmp_path / "coll"
        target_dir.mkdir()
        target = target_dir / "chunks.db"
        target.write_bytes(b"discarded")
        (target_dir / f"chunks.db{suffix}").write_bytes(b"discarded sidecar")

        restore_chunks_db_file(source, target)

        assert target.read_bytes() == source.read_bytes()
        assert _names_in(target_dir) == ["chunks.db"]

    def test_non_file_sidecar_fails_loudly_and_keeps_target(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "healthy.db"
        source.write_bytes(b"healthy content " * 64)
        target_dir = tmp_path / "coll"
        target_dir.mkdir()
        target = target_dir / "chunks.db"
        target.write_bytes(b"discarded")
        (target_dir / "chunks.db-journal").mkdir()

        with pytest.raises(OSError, match="not a regular file"):
            restore_chunks_db_file(source, target)

        assert target.read_bytes() == b"discarded"
        assert _names_in(target_dir) == ["chunks.db", "chunks.db-journal"]

    def test_metadata_restore_leaves_json_sidecar_names_alone(
        self, tmp_path: Path
    ) -> None:
        healthy_dir = tmp_path / "healthy"
        healthy_dir.mkdir()
        (healthy_dir / _METADATA_NAME).write_text('{"c": "old"}')
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        (target_dir / f"{_METADATA_NAME}-journal").write_text("unrelated")

        restore_metadata_files(healthy_dir, target_dir)

        assert (target_dir / f"{_METADATA_NAME}-journal").read_text() == "unrelated"


class TestRestoreThroughThePublishedGate:
    def test_missing_store_restored_with_its_metadata(self, tmp_path: Path) -> None:
        published, source = _published_and_source(tmp_path)

        gate = run_integrity_gate_against_published(
            str(source),
            str(published),
            restore_missing_stores=True,
        )

        assert len(gate.failures) == 1
        failure = gate.failures[0]
        assert failure.store_missing is True
        assert failure.self_heal_succeeded is True, failure.self_heal_error
        assert failure.metadata_restore_error is None
        restored_db = source / ".code-indexer" / "index" / "coll" / "chunks.db"
        published_db = published / ".code-indexer" / "index" / "coll" / "chunks.db"
        assert restored_db.read_bytes() == published_db.read_bytes()
        restored_meta = source / ".code-indexer" / _METADATA_NAME
        assert json.loads(restored_meta.read_text())["current_commit"] == "old_sha"


class TestAtomicSingleFileRestore:
    def test_copy_failure_keeps_previous_target_and_leaves_no_temp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "healthy.db"
        source.write_bytes(b"healthy content " * 64)
        target_dir = tmp_path / "coll"
        target_dir.mkdir()
        target = target_dir / "chunks.db"
        target.write_bytes(b"previous content")
        # A stale temp from an earlier interrupted restore is present too.
        (target_dir / ".chunks.db.restore-tmp").write_bytes(b"garbage")
        _fail_copy_halfway(monkeypatch)

        with pytest.raises(OSError) as raised:
            restore_chunks_db_file(source, target)

        assert raised.value.errno == errno.ENOSPC
        assert target.read_bytes() == b"previous content"
        assert _names_in(target_dir) == ["chunks.db"]

    def test_copy_failure_on_missing_target_leaves_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "healthy.db"
        source.write_bytes(b"healthy content " * 64)
        target_dir = tmp_path / "coll"
        target_dir.mkdir()
        _fail_copy_halfway(monkeypatch)

        with pytest.raises(OSError):
            restore_chunks_db_file(source, target_dir / "chunks.db")

        assert _names_in(target_dir) == []

    def test_restored_file_matches_source_with_its_mode_owned_by_this_process(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "healthy.db"
        source.write_bytes(os.urandom(3 * 1024 * 1024 + 17))
        source.chmod(0o640)
        target_dir = tmp_path / "coll"
        target_dir.mkdir()
        target = target_dir / "chunks.db"
        target.write_bytes(b"corrupt")

        restore_chunks_db_file(source, target)

        assert target.read_bytes() == source.read_bytes()
        stat = target.stat()
        assert stat.st_mode & 0o777 == 0o640
        assert stat.st_uid == os.getuid()
        assert _names_in(target_dir) == ["chunks.db"]

    def test_stale_temp_from_interrupted_restore_is_replaced_not_left_behind(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "healthy.db"
        source.write_bytes(os.urandom(64 * 1024 + 3))
        target_dir = tmp_path / "coll"
        target_dir.mkdir()
        target = target_dir / "chunks.db"
        target.write_bytes(b"corrupt")
        # A restore killed mid-copy left its temp file behind.
        (target_dir / ".chunks.db.restore-tmp").write_bytes(b"garbage " * 1024)

        restore_chunks_db_file(source, target)

        assert target.read_bytes() == source.read_bytes()
        assert list(target_dir.glob("*.restore-tmp")) == []
        assert _names_in(target_dir) == ["chunks.db"]

    def test_metadata_files_restored_without_temp_leftovers(
        self, tmp_path: Path
    ) -> None:
        healthy_dir = tmp_path / "healthy"
        healthy_dir.mkdir()
        (healthy_dir / "metadata-voyage-ai.json").write_text('{"c": "old_a"}')
        (healthy_dir / "metadata-cohere.json").write_text('{"c": "old_b"}')
        (healthy_dir / "config.json").write_text("{}")
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        (target_dir / "metadata-voyage-ai.json").write_text('{"c": "new_a"}')

        restored = restore_metadata_files(healthy_dir, target_dir)

        assert [p.name for p in restored] == [
            "metadata-cohere.json",
            "metadata-voyage-ai.json",
        ]
        assert (target_dir / "metadata-voyage-ai.json").read_text() == '{"c": "old_a"}'
        assert (target_dir / "metadata-cohere.json").read_text() == '{"c": "old_b"}'
        assert _names_in(target_dir) == [
            "metadata-cohere.json",
            "metadata-voyage-ai.json",
        ]

    def test_gate_reports_failed_copy_without_touching_corrupt_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        healthy_index_dir = tmp_path / "published" / ".code-indexer" / "index"
        _make_chunks_db_collection(healthy_index_dir / "coll", _real_records(50))
        source_index_dir = tmp_path / "source" / ".code-indexer" / "index"
        source_db = _make_chunks_db_collection(
            source_index_dir / "coll", _real_records(50)
        )
        _flip_bytes_at_midpoint(source_db)
        corrupt_bytes = source_db.read_bytes()
        names_before = _names_in(source_db.parent)
        _fail_copy_halfway(monkeypatch)

        result = run_refresh_integrity_gate(
            source_index_dir=source_index_dir,
            healthy_index_dir=healthy_index_dir,
        )

        failure = result.failures[0]
        assert failure.self_heal_attempted is True
        assert failure.self_heal_succeeded is False
        assert failure.self_heal_error is not None
        assert "No space left on device" in failure.self_heal_error
        assert source_db.read_bytes() == corrupt_bytes
        assert _names_in(source_db.parent) == names_before
