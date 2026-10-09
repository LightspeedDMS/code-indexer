"""Self-tests for the read-only index/metadata inspector."""

import json
import sqlite3
import subprocess
import sys
import textwrap

import pytest
import zstandard

from index_inspector import (
    index_snapshot,
    inflight_bound,
    metadata_summary,
    parse_progress_json,
    pending_keys,
)

# Schema as created by code_indexer.storage.sqlite_chunk_store (observed on disk).
_SCHEMA = (
    "CREATE TABLE chunks (point_id TEXT PRIMARY KEY, path TEXT, vector BLOB NOT NULL,"
    " data BLOB NOT NULL, type TEXT)"
)


def _make_store(repo, rows):
    db = repo / ".code-indexer" / "index" / "voyage-code-3" / "chunks.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute(_SCHEMA)
    conn.executemany("INSERT INTO chunks VALUES (?, ?, x'00', x'00', ?)", rows)
    conn.commit()
    conn.close()


def test_index_snapshot_counts_content_points_and_paths(tmp_path):
    _make_store(
        tmp_path,
        [
            ("p1", "a.md", "content"),
            ("p2", "a.md", "content"),
            ("p3", "b.md", "content"),
            ("m1", None, "metadata"),
        ],
    )
    snap = index_snapshot(tmp_path)
    assert (snap.layout, snap.points, snap.paths) == ("chunks_db", 3, {"a.md", "b.md"})
    assert snap.path_counts == {"a.md": 2, "b.md": 1}
    assert snap.points_for({"a.md", "zz.md"}) == 2


_DIE_MID_TRANSACTION = textwrap.dedent(
    """
    import os, signal, sqlite3, sys
    conn = sqlite3.connect(sys.argv[1], isolation_level=None)
    conn.execute("PRAGMA cache_size=1")
    conn.execute("BEGIN")
    rows = [("q%d" % i,) for i in range(500)]
    sql = "INSERT INTO chunks VALUES (?, 'b.md', zeroblob(4096), x'00', 'content')"
    conn.executemany(sql, rows)
    os.kill(os.getpid(), signal.SIGKILL)
    """
)


def test_index_snapshot_with_hot_journal_reads_committed_rows_and_leaves_files_untouched(
    tmp_path,
):
    _make_store(tmp_path, [("p1", "a.md", "content")])
    db = tmp_path / ".code-indexer" / "index" / "voyage-code-3" / "chunks.db"
    subprocess.run([sys.executable, "-c", _DIE_MID_TRANSACTION, str(db)])
    journal = db.with_name("chunks.db-journal")
    assert journal.exists() and journal.stat().st_size > 0
    before = (db.read_bytes(), journal.read_bytes())
    snap = index_snapshot(tmp_path)
    assert snap.path_counts == {"a.md": 1}
    assert (db.read_bytes(), journal.read_bytes()) == before


def test_index_snapshot_collects_content_hashes_when_asked(tmp_path):
    db = tmp_path / ".code-indexer" / "index" / "voyage-code-3" / "chunks.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute(_SCHEMA)
    for point_id, digest in (("p1", "h1"), ("p2", "h2")):
        blob = zstandard.ZstdCompressor().compress(
            json.dumps({"payload": {"content_hash": digest, "path": "a.md"}}).encode()
        )
        conn.execute(
            "INSERT INTO chunks VALUES (?, 'a.md', x'00', ?, 'content')",
            (point_id, blob),
        )
    conn.commit()
    conn.close()
    assert index_snapshot(tmp_path, with_hashes=True).content_hashes == {"h1", "h2"}
    assert index_snapshot(tmp_path).content_hashes == set()


def test_index_snapshot_without_a_collection(tmp_path):
    snap = index_snapshot(tmp_path)
    assert (snap.layout, snap.points, snap.paths) == ("none", 0, set())


def test_index_snapshot_refuses_sharded_json(tmp_path):
    shard = (
        tmp_path / ".code-indexer" / "index" / "voyage-code-3" / "ab" / "vector_1.json"
    )
    shard.parent.mkdir(parents=True)
    shard.write_text("{}")
    with pytest.raises(RuntimeError, match="sharded"):
        index_snapshot(tmp_path)


def test_metadata_summary(tmp_path):
    meta = tmp_path / ".code-indexer" / "metadata-voyage-ai.json"
    meta.parent.mkdir(parents=True)
    meta.write_text(
        json.dumps(
            {
                "status": "in_progress",
                "total_files_to_index": 9,
                "files_processed": 2,
                "completed_files": ["x", "y"],
                "failed_files": 0,
                "chunks_indexed": 4,
                "git_available": False,
                "run_sequence": 3,
                "resume_seal": "ab",
            }
        )
    )
    summary = metadata_summary(tmp_path)
    assert summary == {
        "status": "in_progress",
        "total_files_to_index": 9,
        "files_processed": 2,
        "completed_files": 2,
        "failed_files": 0,
        "chunks_indexed": 4,
        "git_available": False,
        "run_sequence": 3,
        "sealed": True,
    }


def test_metadata_summary_missing_file_is_empty(tmp_path):
    assert metadata_summary(tmp_path) == {}


def test_parse_progress_json():
    text = "\n".join(
        [
            "not json",
            json.dumps({"current": 0, "total": 5, "info": "0/5 files (0%) | x"}),
            json.dumps({"current": 3, "total": 5, "info": "3/5 files (60%) | x"}),
            json.dumps({"current": 7, "total": 9, "info": "Embedding 7/9"}),
            json.dumps(
                {
                    "current": 5,
                    "total": 5,
                    "info": "5/5 files (100%) | 0 threads | ✅ Completed",
                }
            ),
        ]
    )
    progress = parse_progress_json(text)
    assert progress.file_totals == [5]
    assert progress.last_file_current == 5
    assert progress.completed is True


def test_parse_progress_json_without_file_lines():
    progress = parse_progress_json("")
    assert (progress.file_totals, progress.last_file_current, progress.completed) == (
        [],
        None,
        False,
    )


def _payload_store(repo, rows):
    db = repo / ".code-indexer" / "index" / "voyage-code-3" / "chunks.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute(_SCHEMA)
    for point_id, payload in rows:
        blob = zstandard.ZstdCompressor().compress(
            json.dumps({"payload": payload}).encode()
        )
        conn.execute(
            "INSERT INTO chunks VALUES (?, ?, x'00', ?, 'content')",
            (point_id, payload.get("path"), blob),
        )
    conn.commit()
    conn.close()


def test_snapshot_exposes_point_ids_and_branch_visibility(tmp_path):
    _payload_store(
        tmp_path,
        [
            ("p1", {"path": "a.md", "content_hash": "h1"}),
            (
                "p2",
                {"path": "b.md", "content_hash": "h2", "hidden_branches": ["feature"]},
            ),
        ],
    )
    snap = index_snapshot(tmp_path, with_hashes=True)
    assert snap.point_ids == {"p1", "p2"}
    assert snap.hidden_ids_for("feature") == {"p2"}
    assert snap.hidden_ids_for("main") == set()
    assert snap.hidden_ids_for(None) == set()


def _pending_db(repo, keys, name="voyage-code-3.pending.db"):
    path = repo / ".code-indexer" / "index" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE pending_vectors (content_key TEXT PRIMARY KEY, model TEXT NOT NULL,"
        " dim INTEGER NOT NULL, vector BLOB NOT NULL, created_at REAL NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO pending_vectors VALUES (?, 'voyage-code-3', 1, x'00', 0)",
        [(k,) for k in keys],
    )
    conn.commit()
    conn.close()
    return path


def test_pending_keys_without_a_pending_store_is_empty(tmp_path):
    assert pending_keys(tmp_path) == set()


def test_pending_keys_reads_content_keys(tmp_path):
    _pending_db(tmp_path, ["k1", "k2"])
    assert pending_keys(tmp_path) == {"k1", "k2"}


def test_pending_keys_with_hot_journal_reads_a_copy_and_leaves_files_untouched(
    tmp_path,
):
    db = _pending_db(tmp_path, ["k1"])
    die = _DIE_MID_TRANSACTION.replace(
        "INSERT INTO chunks VALUES (?, 'b.md', zeroblob(4096), x'00', 'content')",
        "INSERT INTO pending_vectors VALUES (?, 'm', 1, zeroblob(4096), 0)",
    )
    subprocess.run([sys.executable, "-c", die, str(db)])
    journal = db.with_name(db.name + "-journal")
    assert journal.exists() and journal.stat().st_size > 0
    before = (db.read_bytes(), journal.read_bytes())
    assert pending_keys(tmp_path) == {"k1"}
    assert (db.read_bytes(), journal.read_bytes()) == before


def test_pending_store_without_the_table_fails_loudly(tmp_path):
    path = tmp_path / ".code-indexer" / "index" / "voyage-code-3.pending.db"
    path.parent.mkdir(parents=True)
    sqlite3.connect(path).close()
    with pytest.raises(sqlite3.OperationalError):
        pending_keys(tmp_path)


def test_inflight_bound_from_config(tmp_path):
    cfg = tmp_path / ".code-indexer" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps({"voyage_ai": {"parallel_requests": 4, "batch_size": 64}})
    )
    assert inflight_bound(tmp_path) == 256
