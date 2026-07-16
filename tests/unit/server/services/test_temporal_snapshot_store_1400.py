"""
Tests for Story #1400 Phase 7: PayloadCache correctness for temporal
snapshots.

FINAL LOCKED DESIGN (Codex's read-back-with-write-id verification adopted
over Opus's has_key-only check -- strictly more correct: the PostgreSQL
PayloadCache backend catches and suppresses store failures, so a PRIOR
successful write under the same key makes has_key() return True even when
the NEWEST write silently no-op'd; write_id comparison catches exactly
this false-negative).

- Every snapshot write is store_with_key(key, json.dumps(...)) -- never the
  bare store() (which generates a new UUID and cannot upsert a caller-
  chosen key).
- Every write includes write_id (fresh UUID), snapshot_version=1, terminal
  (bool).
- After every write (intermediate AND final), read back and verify the
  write_id matches what was just written.
- Final-write verification failure is JOB-FATAL: raises
  TemporalSnapshotPersistenceError.
- Multi-page reassembly on read (loop retrieve(key, page=n) while
  has_more, concatenate, json.loads).

Real SQLite-backed PayloadCache used throughout (anti-mock) except for the
one test proving write-id verification catches a silently-swallowed write
-- that specific failure mode (PG backend suppressing store errors) is
reproduced with a minimal stub backend standing in for the suppressing PG
backend, since exercising the real PG failure-suppression codepath needs a
live PostgreSQL outage which is out of scope for a unit test.

TDD: written BEFORE implementation.
"""

import json
import tempfile
from pathlib import Path

import pytest

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.services.temporal_snapshot_store import (
    TemporalSnapshotPersistenceError,
    read_temporal_snapshot,
    store_temporal_snapshot,
    temporal_snapshot_key,
)


@pytest.fixture
def payload_cache():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "payload_cache.db"
        config = PayloadCacheConfig()
        cache = PayloadCache(db_path=db_path, config=config)
        cache.initialize()
        yield cache
        cache.close()


class TestTemporalSnapshotKey:
    def test_key_format(self):
        assert temporal_snapshot_key("job-123") == "temporal_query:job-123"


class TestStoreAndReadRoundTrip:
    def test_store_then_read_returns_same_results(self, payload_cache):
        snapshot = {
            "results": [{"file_path": "a.py"}],
            "shards_completed": 1,
            "shards_total": 2,
            "ctx": {"requested_limit": 10},
        }
        store_temporal_snapshot(payload_cache, "job-1", snapshot, terminal=False)

        read_back = read_temporal_snapshot(payload_cache, "job-1")
        assert read_back is not None
        assert read_back["results"] == snapshot["results"]
        assert read_back["shards_completed"] == 1

    def test_read_missing_job_returns_none(self, payload_cache):
        assert read_temporal_snapshot(payload_cache, "does-not-exist") is None


class TestEnvelopeFields:
    def test_envelope_has_write_id_version_and_terminal(self, payload_cache):
        snapshot = {
            "results": [],
            "shards_completed": 0,
            "shards_total": None,
            "ctx": {},
        }
        store_temporal_snapshot(payload_cache, "job-2", snapshot, terminal=False)

        read_back = read_temporal_snapshot(payload_cache, "job-2")
        assert "write_id" in read_back
        assert read_back["snapshot_version"] == 1
        assert read_back["terminal"] is False

    def test_upsert_uses_store_with_key_not_bare_store(self, payload_cache):
        """A second write to the SAME job_id must overwrite (upsert), not
        create a second entry under a fresh UUID -- proves store_with_key
        semantics, not bare store()."""
        snap1 = {
            "results": [{"file_path": "a.py"}],
            "shards_completed": 1,
            "shards_total": 2,
            "ctx": {},
        }
        snap2 = {
            "results": [{"file_path": "a.py"}, {"file_path": "b.py"}],
            "shards_completed": 2,
            "shards_total": 2,
            "ctx": {},
        }

        store_temporal_snapshot(payload_cache, "job-3", snap1, terminal=False)
        store_temporal_snapshot(payload_cache, "job-3", snap2, terminal=True)

        read_back = read_temporal_snapshot(payload_cache, "job-3")
        assert read_back["shards_completed"] == 2
        assert read_back["terminal"] is True


class TestMultiPageReassembly:
    def test_large_snapshot_reassembled_across_pages(self, payload_cache):
        """A snapshot exceeding one page's max_fetch_size_chars must still
        round-trip correctly via multi-page reassembly."""
        # config default max_fetch_size_chars=5000 -- build a snapshot whose
        # JSON serialization comfortably exceeds that.
        big_results = [
            {"file_path": f"file_{i}.py", "content": "x" * 100} for i in range(200)
        ]
        snapshot = {
            "results": big_results,
            "shards_completed": 5,
            "shards_total": 5,
            "ctx": {},
        }
        assert len(json.dumps(snapshot)) > 5000

        store_temporal_snapshot(payload_cache, "job-4", snapshot, terminal=True)
        read_back = read_temporal_snapshot(payload_cache, "job-4")

        assert len(read_back["results"]) == 200
        assert read_back["results"][199]["file_path"] == "file_199.py"


class _SuppressingStubBackend:
    """Minimal stand-in for the PG PayloadCache backend's confirmed
    failure-suppression behavior (payload_cache_backend.py catches and
    swallows store() exceptions, returning normally). Simulates: the FIRST
    write (intermediate checkpoint) succeeds and is retrievable; the SECOND
    write (the "final" write under test) is silently swallowed -- the row
    already present from write #1 is left untouched, so a bare has_key()
    check would still return True even though the final write never
    happened. This is exactly the false-negative Codex's write-id
    verification catches and Opus's has_key-only check cannot.
    """

    def __init__(self):
        self._rows: dict = {}
        self._write_count = 0

    def store(self, key, content, preview, ttl_seconds):
        self._write_count += 1
        if self._write_count >= 2:
            return  # silently swallowed, mirrors the real PG backend bug
        self._rows[key] = {"content": content, "preview": preview}

    def retrieve(self, key):
        return self._rows.get(key)


class TestWriteIdVerificationCatchesSilentFailure:
    def test_final_write_verification_failure_is_job_fatal(self, payload_cache):
        """Reproduces the exact false-negative the locked design's HIGH item
        warns about: has_key() alone cannot distinguish "the final write
        succeeded" from "an old checkpoint is still sitting there and the
        final write silently no-op'd". write_id comparison catches it."""
        payload_cache._backend = _SuppressingStubBackend()  # type: ignore[attr-defined]

        intermediate = {
            "results": [],
            "shards_completed": 0,
            "shards_total": None,
            "ctx": {},
        }
        store_temporal_snapshot(payload_cache, "job-5", intermediate, terminal=False)

        final = {
            "results": [{"file_path": "a.py"}],
            "shards_completed": 1,
            "shards_total": 1,
            "ctx": {},
        }
        with pytest.raises(TemporalSnapshotPersistenceError):
            store_temporal_snapshot(payload_cache, "job-5", final, terminal=True)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
